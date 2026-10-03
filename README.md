<div align="center">

<img src="logo.png" alt="Segmentarr Logo" width="220">

# Segmentarr

**Clean, stabilize, and repair IPTV streams before Dispatcharr ever sees them.**

</div>

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that cleans up unstable IPTV provider streams (Xtream Codes or plain URL) before Dispatcharr ever sees them.

Instead of piping the provider straight through, Segmentarr cuts it into short HLS segments, repairs timestamp corruption segment by segment, and hands clean MPEG-TS through a final cvlc stage (the same tail Profilarr uses) to Dispatcharr over `pipe:1`.

```
provider (XC / URL)
   |  ffmpeg: -c copy -f hls   (keyframe-aligned segments in /dev/shm)
   v
supervisor: validate -> resync -> drop corrupt packets -> stitch PCR/PTS/DTS
   |  ffmpeg finalizer (copy)
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
- **Dead connections** - ffmpeg reconnects on drops, and the supervisor restarts ingest if segments stop arriving.

## Requirements

- Dispatcharr with plugin support
- `ffmpeg`, `python3` and `cvlc` inside the Dispatcharr container (`cvlc` is only needed when CVLC Network Cache is not Off)
- A tmpfs at `/dev/shm` (falls back to `/tmp`; override with `SEGMENTARR_TMP`)

## Install

1. Download `segmentarr.zip` from the [latest release](https://github.com/Tw1zT3d2four7/Segmentarr/releases/latest).
2. In Dispatcharr, open **Plugins**, import the zip, and enable **Segmentarr**.
3. Choose your settings and press **Apply & Synchronize**.
4. Restart any channel that is already playing.

Apply creates a `Segmentarr Profile - ...` stream profile and a matching `Segmentarr Output - ...` output profile, and makes both the defaults. Older unlocked Segmentarr profiles are replaced; locked ones are left alone.

> Segmentarr and Profilarr both set the same default stream and output profiles. Whichever one you press Apply on last owns them.

## Settings

| Setting | Default | Effect |
|---|---|---|
| Segment Profile | Standard (2s) | Segment length: Standard 2s, Low Latency 1s, Resilient 4s (waits for two segments before starting). |
| CVLC Network Cache | 1000 ms | Puts cvlc as the last stage before Dispatcharr with this caching. Off removes cvlc. |
| Stall Timeout | 25s | Restart the provider connection if no segment appears for this long. |
| Max Catch-up Backlog | 20s | Queue beyond this value is dropped to jump to live. |
| Timeline Gap Tolerance | 1s | Forward PCR jumps up to this are kept; larger breaks are stitched. |
| Reconnect Delay Ceiling | 5s | Longest backoff between provider reconnects. |
| Provider I/O Timeout | 15s | Provider connection treated as dead after this long without data. |
| Stream Probe Time | 3s | How much stream ffmpeg analyses before starting. |
| Audio Transcoding Override | AAC | Audio codec applied by the Output Profile (AAC, AC3, E-AC3, Opus, MP3, Copy). |

The stream stage always copies video and audio. Audio is transcoded once, in the Output Profile. Settings are baked into the wrapper scripts at Apply time, so re-apply and restart the channel after changing any of them.

## Verify it's working

```sh
docker exec dispatcharr sh -c "ps -eo pid,ppid,args | grep -E 'segmentarr|ffmpeg' | grep -v grep"
```

You should see the supervisor with an ingest ffmpeg writing `/dev/shm/segmentarr/<pid>/seg_*.ts`, a finalizer ending in `-c:a copy ... pipe:1`, a `vlc -I dummy ... fd://0` process (cvlc runs as `vlc`), and Dispatcharr's output-stage ffmpeg after it. The supervisor logs its settings on its first line, plus `timeline break`, and `skipped to live` events.

If the output stage doesn't match your Segmentarr Output Profile, the client that started the channel is probably using a built-in profile (for example *Web Player*). Start the channel from the client you actually use.

## Tradeoffs

- Adds roughly one segment plus a keyframe wait of latency, plus the CVLC cache.
- RAM use is small: 30s of an 8 Mbps stream is about 30 MB.

## Files

| File | Purpose |
|---|---|
| `plugin.py` | Dispatcharr plugin: settings, wrapper scripts, profile creation |
| `segmentarr-supervisor.py` | Runs the ffmpeg stages and cvlc, plus timeline healing |
| `plugin.json` | Plugin metadata |
| `logo.png` | Segmentarr plugin logo |

Releases are built by tagging `vX.Y.Z`; the workflow checks that the tag, `plugin.py`, and `plugin.json` all carry the same version.

## License

MIT
