<p align="center"><img src="logo.png" width="160" alt="Segmentarr logo"></p>

# Segmentarr

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that cleans up unstable IPTV provider streams (Xtream Codes or plain URL) before Dispatcharr ever sees them.

Instead of piping the provider straight through, Segmentarr cuts it into short HLS segments, repairs timestamp corruption segment by segment, and hands clean MPEG-TS through a final cvlc stage (the same tail Profilarr uses) to Dispatcharr over `pipe:1`.

```
provider (XC / URL)
   |  ffmpeg: -c copy -f hls   (keyframe-aligned segments in /dev/shm)
   v
supervisor: validate -> resync -> drop corrupt packets -> stitch PCR/PTS/DTS
   |  ffmpeg finalizer (video copy + selected audio mode)
   v
cvlc (configurable caching)  ->  pipe:1
   v
Dispatcharr Output Profile (audio stage)  ->  clients
```

## What it fixes

- **Timestamp breaks** - backward or forward PCR/PTS/DTS jumps are stitched onto one continuous clock. Healthy streams pass through byte-identical.
- **Corruption** - misaligned bytes are resynced and transport-error packets are replaced with null packets.
- **Provider stalls** - cvlc's network cache smooths short provider hiccups.
- **Falling behind** - if the queue grows past the catch-up limit it jumps back to live.
- **Provider stalls** - the supervisor restarts ingest if segments stop arriving.

## Requirements

- Dispatcharr with plugin support
- `ffmpeg`, `python3` and `cvlc` inside the Dispatcharr container (`cvlc` is only needed when CVLC Network Cache is not Off)
- A tmpfs at `/dev/shm` (falls back to `/tmp`; override with `SEGMENTARR_TMP`)

## Install

1. Download `segmentarr.zip` from the [latest release](https://github.com/Tw1zT3d2four7/Segmentarr/releases/latest).
2. In Dispatcharr, open **Plugins**, import the zip, and enable **Segmentarr**.
3. Choose your settings and press **Apply & Synchronize**.
4. Restart any channel that is already playing.

Apply creates compact `Segarr | ...` and `SegOut | ...` profile names so the active stream/profile display stays readable in Dispatcharr. For example: `Segarr | Std2s | CV1s | AAC`. Older unlocked Segmentarr profiles are replaced; locked ones are left alone. `Std2s` = Standard 2s, `LL1s` = Low Latency 1s, `Res4s` = Resilient 4s, and `CV1s` means a 1-second CVLC cache.

> Segmentarr and Profilarr both set the same default stream and output profiles. Whichever one you press Apply on last owns them.

## Settings

| Setting | Default | Effect |
|---|---|---|
| Segment Profile | Standard (2s) | Segment length: Standard 2s, Low Latency 1s, Resilient 4s (waits for two segments before starting). |
| CVLC Network Cache | 1000 ms | Puts cvlc as the last stage before Dispatcharr with this caching. Off removes cvlc. |
| Stall Timeout | 25s | Restart the provider connection if no segment appears for this long. |
| Max Catch-up Backlog | 20s | Queue beyond this value is dropped to jump to live. |
| Timeline Gap Tolerance | 1s | Forward PCR jumps up to this are kept; larger breaks are stitched. |
| Provider I/O Timeout | 15s | Provider connection treated as dead after this long without data. |
| Stream Probe Time | 3s | How much stream ffmpeg analyses before starting. |
| Audio Transcoding Override | AAC | Audio codec applied by the Output Profile (AAC, AC3, E-AC3, Opus, MP3, Copy). |

The ingest stage always copies provider video and audio into HLS segments. The finalizer then copies video and applies the selected audio mode once (AAC, AC3, E-AC3, Opus, MP3, or Copy). The matching Dispatcharr Output Profile is generated with the same selected audio parameters, so both stages stay synchronized. Settings are written into the stream profile's Parameters at Apply time, so re-apply and restart the channel after changing any of them. The profile runs `python3 segmentarr-supervisor.py` directly (no wrapper scripts), and reloading or updating the plugin never interrupts channels that are already playing.

## Verify it's working

```sh
docker exec dispatcharr sh -c "ps -eo pid,ppid,args | grep -E 'segmentarr|ffmpeg' | grep -v grep"
```

You should see the supervisor with an ingest ffmpeg writing `/dev/shm/segmentarr/<pid>/seg_*.ts`, a finalizer ending in the selected audio mode (for example `-c:a libmp3lame -b:a 192k -ac 2 ... pipe:1` for MP3), a `vlc -I dummy ... fd://0` process (cvlc runs as `vlc`), and Dispatcharr's output-stage ffmpeg after it. The supervisor logs its settings on its first line, plus `timeline break`, and `skipped to live` events.

If the output stage doesn't match your Segmentarr Output Profile, the client that started the channel is probably using a built-in profile (for example *Web Player*). Start the channel from the client you actually use.

## Logs

Each stream writes to a persistent log that survives plugin updates, independent of what Dispatcharr captures:

```sh
docker exec dispatcharr tail -n 200 /data/segmentarr/logs/segmentarr.log
```

(The file rotates at 5 MB to `segmentarr.log.1`.) Lines carry a date, time, supervisor pid and channel tag (`ch=<stream id>`), and the ffmpeg and cvlc output of every stage is captured in the same file.

| Line | Meaning |
|---|---|
| `segment late: 9.1s (typical 2.0s)` | The provider delivered a segment much later than usual, so the stall is upstream. |
| `downstream slow: write blocked 7.4s` | Dispatcharr, cvlc or the client stopped reading, so the stall is downstream. |
| `hb 60s: segments=... max_gap=... max_write_block=... max_queue=...` | One-minute summary (file only). A high `max_gap` with no `segment late` line is a near-miss. |
| `timeline break (...)` / `skipped to live` / `ingest exited` | The healer stitched a timestamp break, dropped backlog, or restarted the provider connection. |

## Tradeoffs

- Adds roughly one segment plus a keyframe wait of latency, plus the CVLC cache.
- RAM use is small: 30s of an 8 Mbps stream is about 30 MB.

## Files

| File | Purpose |
|---|---|
| `plugin.py` | Dispatcharr plugin: settings, wrapper scripts, profile creation |
| `segmentarr-supervisor.py` | Runs the ffmpeg stages and cvlc, plus timeline healing |
| `plugin.json` | Plugin metadata |

Releases are built by tagging `vX.Y.Z`; the workflow checks that the tag, `plugin.py`, and `plugin.json` all carry the same version.

## License

MIT
