# Segmentarr

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that cleans up unstable IPTV provider streams (Xtream Codes or plain URL) before Dispatcharr ever sees them.

Instead of piping the provider straight through, Segmentarr cuts it into short HLS segments, repairs timestamp corruption segment by segment, holds a configurable buffer, and hands clean MPEG-TS to Dispatcharr over `pipe:1`.

```
provider (XC / URL)
   |  ffmpeg: -c copy -f hls   (keyframe-aligned segments in /dev/shm)
   v
supervisor: validate -> resync -> drop corrupt packets -> stitch PCR/PTS/DTS
            -> paced release from a reserve buffer
   |  ffmpeg finalizer (copy)  ->  pipe:1
   v
Dispatcharr Output Profile (audio stage)  ->  clients
```

## What it fixes

- **Timestamp breaks** - backward or forward PCR/PTS/DTS jumps are stitched onto one continuous clock. Healthy streams pass through byte-identical.
- **Corruption** - misaligned bytes are resynced and transport-error packets are replaced with null packets.
- **Provider stalls** - a reserve of segments is released in real time, so freezes shorter than the buffer never reach the client.
- **Falling behind** - if the queue grows past the catch-up limit it jumps back to live.
- **Dead connections** - ffmpeg reconnects on drops, and the supervisor restarts ingest if segments stop arriving.

## Requirements

- Dispatcharr with plugin support
- `ffmpeg` and `python3` inside the Dispatcharr container (both ship with it)
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
| Segment Profile | Standard (2s) | Segment length: Standard 2s, Low Latency 1s, Resilient 4s (two segments buffered before start). |
| Buffer | 8s | Reserve held before playback and released in real time. Tune time grows by the same amount. |
| After Buffer Underrun | Re-prime | Resume immediately, or refill the buffer first. |
| Stall Timeout | 25s | Restart the provider connection if no segment appears for this long. |
| Max Catch-up Backlog | 20s | Queue beyond Buffer + this value is dropped to jump to live. |
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

You should see the supervisor with an ingest ffmpeg writing `/dev/shm/segmentarr/<pid>/seg_*.ts`, a finalizer ending in `-c:a copy ... pipe:1`, and Dispatcharr's output-stage ffmpeg after it. The supervisor logs its settings on its first line, plus `timeline break`, `buffer underrun`, and `skipped to live` events.

If the output stage doesn't match your Segmentarr Output Profile, the client that started the channel is probably using a built-in profile (for example *Web Player*). Start the channel from the client you actually use.

## Tradeoffs

- Adds roughly one segment plus a keyframe wait of latency, plus the Buffer value.
- RAM use is small: 30s of an 8 Mbps stream is about 30 MB.
- After an underrun with *Resume immediately*, the reserve stays thin until the next stall.

## Files

| File | Purpose |
|---|---|
| `plugin.py` | Dispatcharr plugin: settings, wrapper scripts, profile creation |
| `segmentarr-supervisor.py` | Runs both ffmpeg stages, healing, buffering |
| `plugin.json` | Plugin metadata |

Releases are built by tagging `vX.Y.Z`; the workflow checks that the tag, `plugin.py`, and `plugin.json` all carry the same version.

## License

MIT
