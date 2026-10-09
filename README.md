<p align="center"><img src="logo.png" width="160" alt="Segmentarr logo"></p>

# Segmentarr

A [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr) plugin that cleans up unstable IPTV provider streams (Xtream Codes or plain URL) before Dispatcharr ever sees them.

Instead of piping the provider straight through, Segmentarr cuts it into short HLS segments, repairs timestamp corruption segment by segment, applies the selected audio mode, and hands clean MPEG-TS through an optional CVLC stage to Dispatcharr.

```
provider (XC / URL)
   |  ffmpeg: -c copy -> HLS segments
   v
Segmentarr supervisor
   |  validate -> resync -> repair timeline -> manage stalls/backlog
   v
ffmpeg finalizer
   |  video copy + selected audio mode
   v
optional CVLC caching
   |
   v
Dispatcharr
   |
   v
M3U / HDHR
   |
   v
player / client
```

## What it fixes

- **Timestamp breaks** - backward or forward PCR/PTS/DTS jumps are stitched onto one continuous clock.
- **Corruption** - misaligned bytes are resynced and transport-error packets are replaced with null packets.
- **Provider stalls** - the pipeline monitors segment delivery and restarts ingest when segments stop arriving.
- **Falling behind** - if the queue grows past the catch-up limit, Segmentarr skips forward to live.
- **Short provider hiccups** - the optional CVLC cache can absorb brief interruptions.

## Requirements

- Dispatcharr with plugin support
- `ffmpeg`, `python3`, and `cvlc` inside the Dispatcharr container (`cvlc` is only needed when CVLC Network Cache is not Off)
- A tmpfs at `/dev/shm` (falls back to `/tmp`; override with `SEGMENTARR_TMP`)

## Install

1. Download `segmentarr.zip` from the [latest release](https://github.com/Tw1zT3d2four7/Segmentarr/releases/latest).
2. In Dispatcharr, open **Plugins**, import the zip, and enable **Segmentarr**.
3. Choose your settings.
4. Press **Actions -> Apply & Synchronize**.
5. Restart any channel that was already playing before the profile change.
6. During initial setup, configure M3U/HDHR clients to use Segmentarr's generated Output Profile as described below. Later Segmentarr profile changes reuse that Output Profile ID, so client URLs normally do not need to change.

Apply creates compact `Segarr | ...` and `SegOut | ...` profile names so the active stream/profile display stays readable in Dispatcharr. For example:

```
Segarr | Std2s | CV1s | MP3
SegOut | Std2s | CV1s | MP3
```

`Std2s` = Standard 2s, `LL1s` = Low Latency 1s, `Res4s` = Resilient 4s. The `CV` value identifies the CVLC cache setting.

> Segmentarr and Profilarr both set the same Dispatcharr default stream/output profile settings. Whichever plugin is synchronized last owns those defaults.

## Settings

| Setting | Default | Effect |
|---|---|---|
| Segment Profile | Standard (2s) | Standard 2s, Low Latency 1s, or Resilient 4s segmentation. |
| CVLC Network Cache | 1000 ms | Adds CVLC as the final caching stage with this cache value. Off removes CVLC. |
| Stall Timeout | 25s | Restart the provider connection if no segment appears for this long. |
| Max Catch-up Backlog | 20s | Queue beyond this value is dropped so the stream can jump back to live. |
| Timeline Gap Tolerance | 1s | Forward PCR jumps up to this value are tolerated; larger breaks are stitched. |
| Provider I/O Timeout | 15s | Provider connection is treated as dead after this long without data. |
| Stream Probe Time | 3s | How long the input is analyzed before starting. |
| Audio Transcoding Override | AAC | Selects AAC, AC3, E-AC3, Opus, MP3, or Copy for the final Segmentarr stream. |

### Audio Transcoding Override

The available audio choices are:

| Selection | Segmentarr audio output |
|---|---|
| **AAC** | AAC, 192 kbps, stereo |
| **AC3** | AC3, 192 kbps, stereo |
| **E-AC3** | E-AC3, 192 kbps, stereo |
| **Opus** | Opus, 128 kbps, stereo |
| **MP3** | MP3, 192 kbps, stereo |
| **Copy** | Keeps the provider's original audio codec |

Segmentarr always copies provider video. The finalizer then applies the selected audio mode once.

There are **no NVIDIA, Intel QSV, AMD, or other hardware-video settings in Segmentarr's current UI**. Do not look for them when configuring Segmentarr.

---

# Important: Understanding Dispatcharr Audio Statistics

This is the most important thing to understand when testing the Audio Transcoding Override.

**Dispatcharr can report the provider's original audio codec even when Segmentarr has successfully converted that audio.**

For example, suppose the provider sends AC3:

```
Provider -> AC3
```

You configure Segmentarr:

```
Audio Transcoding Override -> MP3
```

Segmentarr then performs:

```
AC3 -> MP3
```

Dispatcharr can still show an upstream/source statistic such as:

```
Audio: AC3
```

That does **not** prove that Segmentarr failed.

The reliable test is to verify the Segmentarr process and then check the codec received by the **actual downstream player/client**.

For the MP3 example, the expected result is:

```
Provider audio: AC3
        |
        v
Segmentarr: AC3 -> MP3
        |
        v
Dispatcharr output profile: audio copy
        |
        v
Emby / IPTV player: MP3
```

So:

> **Dispatcharr showing the provider's original AC3 is compatible with Segmentarr successfully delivering MP3 to the client.**

---

# Apply & Synchronize

After changing Segmentarr settings, use:

**Actions -> Apply & Synchronize**

This creates the Segmentarr Stream Profile and matching Output Profile on first use. On later synchronizations, Segmentarr updates its existing generated profiles in place, preserving their Dispatcharr-assigned IDs.

The generated profiles normally look like:

```
Segarr | Std2s | CV1s | MP3
SegOut | Std2s | CV1s | MP3
```

The generated Output Profile is important because Segmentarr has already performed the selected audio conversion.

The matching Segmentarr Output Profile uses audio/video copy rather than performing another audio encode. In other words:

```
Provider AC3
   |
   v
Segmentarr
   |
   +-- audio: AC3 -> MP3
   |
   v
Dispatcharr Segmentarr Output Profile
   |
   +-- video: copy
   +-- audio: copy
   |
   v
Client receives MP3
```

This prevents an unnecessary second audio transcode.

After changing settings, re-apply the profile and restart the channel before testing.

---

# M3U / HDHR Setup

**Creating the profiles is not the final step.**

Your M3U or HDHR configuration must actually use the **Segmentarr-generated Output Profile**.

This is especially important when using Emby Live TV or an external IPTV player.

## M3U

**Update the Emby/TiviMate (or other M3U client) URL only during the first-time Segmentarr setup. Do not change or replace that URL after ordinary Segmentarr setting/profile changes.** Segmentarr now updates its existing generated Output Profile in place, so the Output Profile ID in the URL stays the same.

A Dispatcharr M3U URL can explicitly select an Output Profile with:

```
output_profile=<SEGMENTARR_OUTPUT_PROFILE_ID>
```

For example:

```
http://192.168.1.11:9191/output/m3u?tvg_id_source=tvg_id&output_format=mpegts&output_profile=48
```

The `48` in that example is only an example of a profile ID from one installation. **Do not copy that number unless it is the ID of your own Segmentarr-generated Output Profile.**

The important part is:

```
output_profile=YOUR_SEGMENTARR_OUTPUT_PROFILE_ID
```

### Why this matters

If Segmentarr is configured for MP3 but the M3U client is still requesting an older Dispatcharr Output Profile, Dispatcharr can perform another audio conversion after Segmentarr.

For example:

```
Provider AC3
   |
   v
Segmentarr: AC3 -> MP3
   |
   v
OLD Dispatcharr Output Profile
   |
   v
MP3 -> AAC
   |
   v
Client receives AAC
```

That makes it look like Segmentarr ignored the MP3 setting when the real problem is that the client is using the wrong Output Profile.

### First-time setup only

After the first Segmentarr **Apply & Synchronize**, set the URL in each client once so it includes the ID of the Segmentarr-generated Output Profile. This initial URL setup applies to:

- Emby Live TV
- TiviMate
- other IPTV players
- other devices using the Dispatcharr M3U

**After that, keep the same URL.** When Segmentarr settings change, press **Actions -> Apply & Synchronize** and restart the channel; do not edit the client URL just because the generated profile name or parameters changed. The ID is preserved automatically.

Only update the URL again if the Output Profile was manually deleted/lost or another separate action genuinely changed its ID.

---

# HDHR

If you use Dispatcharr through HDHR, make sure the HDHR configuration is using the **Segmentarr-generated Output Profile**.

When **Actions -> Apply & Synchronize** is run, Segmentarr synchronizes the appropriate Dispatcharr stream/output settings. Verify the resulting HDHR configuration is using the Segmentarr-generated Output Profile rather than an older generic profile.

The key requirement is the same as M3U:

> The client must ultimately request/use the Segmentarr Output Profile.

---

# Important: Web Player Output Profile Is Separate

Dispatcharr also has a setting called:

**Web Player Output Profile**

This controls the Output Profile used when previewing streams in Dispatcharr's browser web player.

It does **not** automatically change the Output Profile contained in an M3U URL being used by Emby or another external client.

Think of them as separate paths:

```
Dispatcharr browser preview
        |
        +-> Web Player Output Profile
```

versus:

```
M3U / HDHR client
        |
        +-> configured Output Profile
        |
        v
      Client
```

Changing the Web Player Output Profile alone is therefore not enough to change an existing M3U client's Output Profile.

---

# Profile Changes Keep the Same Output Profile ID

Segmentarr lets Dispatcharr assign the Output Profile ID during initial installation. Segmentarr then reuses that generated profile and updates its name and FFmpeg parameters in place when settings change.

**For normal profile changes, you do not need to update the M3U URL in Emby, TiviMate, or other clients.** The `output_profile=<ID>` value remains valid because the existing Output Profile is updated instead of deleted and recreated.

When you change Segmentarr settings, Apply & Synchronize reuses the existing generated Segmentarr Output Profile record and updates its name and parameters in place. Its Dispatcharr ID is retained, so M3U URLs using `output_profile=<ID>` continue to reference the same profile. A new ID is created only when no generated Segmentarr Output Profile exists yet. This protects IDs going forward; it cannot infer an ID that was already deleted by an older plugin version.

## What to do after a profile change

1. Change the Segmentarr setting.
2. Press **Actions -> Apply & Synchronize**.
3. Restart the channel before testing.
4. Verify the existing `SegOut | ...` profile now has the expected name and parameters.

Do not recreate or replace the M3U URL just because the profile name changed.

---

# Initial M3U / HDHR Setup

Do these steps **once, the first time you install Segmentarr**. You do not need to repeat them after normal Segmentarr setting changes.

1. In Dispatcharr, open the Segmentarr plugin and press **Actions -> Apply & Synchronize**. Wait for it to finish.
2. In Dispatcharr's left menu, open **Settings**, then open **Output Profiles**.
3. Find the profile whose name starts with **`SegOut |`**. This is the Output Profile created by Segmentarr. Do not choose a generic profile such as a default AAC/AC3 profile.
4. Find that profile's numeric **ID** in the Output Profiles list and write it down. For example, if the Segmentarr profile's ID is `48`, the value you need is `48`—not the profile name.
5. In the Dispatcharr M3U URL, add or update the parameter `output_profile=48` using **your own profile's ID**. For example, the URL will contain something like:
   ```
   http://YOUR-DISPATCHARR-ADDRESS:9191/output/m3u?...&output_profile=48
   ```
   Keep the other parts of your existing URL; replace `48` with the ID you found in step 4. Do not copy the example ID unless your own Segmentarr Output Profile really has ID 48.
6. Save that M3U URL in Emby Live TV, TiviMate, or whichever IPTV client you use, then start a channel to verify playback.

If you use HDHR instead of an M3U URL, select the **`SegOut | ...`** Segmentarr Output Profile in the relevant HDHR output-profile setting.

**After this first-time setup, leave the Emby/TiviMate M3U URL alone.** When you change Segmentarr settings later, return to the Segmentarr plugin and press **Actions -> Apply & Synchronize**, then restart the channel. Segmentarr updates the existing Output Profile in place and preserves its ID, so the URL should not need to change. Only update the URL if the Output Profile was manually deleted/lost or its ID was changed by some other action.

---

# Verify Segmentarr Is Actually Running

Start a channel through the client you actually use, then run:

```bash
sudo docker exec dispatcharr ps auxww | grep -E 'segmentarr-supervisor|ffmpeg' | grep -v grep
```

This shows the active Segmentarr supervisor and FFmpeg processes.

You should see the Segmentarr supervisor and its FFmpeg stages.

---

# Verify the Selected Audio Mode

The Segmentarr supervisor command contains the selected audio mode.

For example, if you selected MP3, look for:

```
standard mp3
```

A live command may look similar to:

```
segmentarr-supervisor.py ... standard mp3 ... <user-agent> <stream-url>
```

The exact command will differ depending on your settings.

The important part is that the selected audio value is present.

Examples:

| Segmentarr selection | Supervisor should contain |
|---|---|
| AAC | `aac` |
| AC3 | `ac3` |
| E-AC3 | `eac3` |
| Opus | `opus` |
| MP3 | `mp3` |
| Copy | `copy` |

---

# Verify the FFmpeg Audio Encoder

The finalizer should show the encoder corresponding to your selected audio mode.

### AAC

Look for:

```
-c:a aac
```

Expected client audio:

```
AAC
```

### AC3

Look for:

```
-c:a ac3
```

Expected client audio:

```
AC3
```

### E-AC3

Look for:

```
-c:a eac3
```

Expected client audio:

```
E-AC3
```

### Opus

Look for:

```
-c:a libopus
```

Expected client audio:

```
Opus
```

### MP3

Look for:

```
-c:a libmp3lame
```

Expected client audio:

```
MP3
```

For the current MP3 profile, the encoder should also show the 192 kbps stereo settings:

```
-c:a libmp3lame -b:a 192k -ac 2
```

### Copy

Look for:

```
-c:a copy
```

The client should receive the provider's original audio codec.

---

# The Segmentarr Output Profile Should Copy the Finished Audio

Once Segmentarr has produced the selected audio format, the matching Dispatcharr Output Profile should preserve it.

The important options are:

```
-c:v copy -c:a copy
```

For example, with MP3:

```
Provider AC3
      |
      v
Segmentarr finalizer
      |
      +-- -c:v copy
      +-- -c:a libmp3lame -b:a 192k -ac 2
      |
      v
MP3
      |
      v
Dispatcharr Segmentarr Output Profile
      |
      +-- -c:v copy
      +-- -c:a copy
      |
      v
Client
      |
      v
MP3
```

If another downstream Dispatcharr FFmpeg process is using something such as:

```
-c:a aac
```

then a second audio transcode is occurring.

---

# Complete MP3 Test Example

Assume your provider sends AC3.

You select:

```
Audio Transcoding Override: MP3
```

Then press:

**Actions -> Apply & Synchronize**

Your profiles might look like:

```
Segarr | Std2s | CV1s | MP3
SegOut | Std2s | CV1s | MP3
```

You make sure your M3U/HDHR configuration uses the Segmentarr Output Profile.

Then start a channel and run:

```bash
sudo docker exec dispatcharr ps auxww | grep -E 'segmentarr-supervisor|ffmpeg' | grep -v grep
```

You should find:

```
... segmentarr-supervisor.py ... standard mp3 ...
```

and the finalizer should contain:

```
-c:a libmp3lame -b:a 192k -ac 2
```

The matching Dispatcharr Output Profile should preserve the result with:

```
-c:a copy
```

Dispatcharr's upstream/source statistics may still say:

```
Audio: AC3
```

That is okay.

The actual downstream player should report:

```
Audio: MP3
```

**That is the successful result.**

---

# Expected Results for Every Audio Setting

| Segmentarr setting | Finalizer encoder | What the downstream player should report |
|---|---|---|
| **AAC** | `-c:a aac -b:a 192k -ac 2` | AAC |
| **AC3** | `-c:a ac3 -b:a 192k -ac 2` | AC3 |
| **E-AC3** | `-c:a eac3 -b:a 192k -ac 2` | E-AC3 |
| **Opus** | `-c:a libopus -b:a 128k -ac 2` | Opus |
| **MP3** | `-c:a libmp3lame -b:a 192k -ac 2` | MP3 |
| **Copy** | `-c:a copy` | Provider's original codec |

The Dispatcharr Output Profile should then preserve the finished audio with:

```
-c:a copy
```

---

# Troubleshooting

## Dispatcharr says AC3 but my player says MP3

**This is a successful result.**

The provider is sending AC3, Segmentarr is converting it to MP3, and the downstream client is receiving MP3.

---

## Segmentarr says MP3 but my player says AAC

Check the M3U/HDHR Output Profile.

The most likely problem is that the client is using a different Dispatcharr Output Profile after Segmentarr has already produced MP3.

For M3U, verify:

```
output_profile=<CURRENT_SEGMENTARR_OUTPUT_PROFILE_ID>
```

Then run:

```bash
sudo docker exec dispatcharr ps auxww | grep -E 'segmentarr-supervisor|ffmpeg' | grep -v grep
```

If Segmentarr shows:

```
-c:a libmp3lame
```

but a later Dispatcharr FFmpeg shows:

```
-c:a aac
```

then you have a second audio transcode.

---

## I changed the Segmentarr profile but the client still gets the old audio

Repeat this process:

1. Confirm the intended Segmentarr setting is selected.
2. Press **Actions -> Apply & Synchronize**.
3. Restart the channel.
4. Verify the Segmentarr supervisor and finalizer use the expected audio encoder.
5. Verify the downstream client receives the selected codec.

Do not change the M3U URL for an ordinary profile change. If the generated Output Profile was manually deleted or lost, synchronize again, find the newly assigned ID, and then update the client URL.

---

# Quick Verification Checklist

Before concluding that Segmentarr's audio override is not working, check:

```
[ ] Correct Audio Transcoding Override selected

[ ] Actions -> Apply & Synchronize completed

[ ] Segmentarr Stream Profile exists

[ ] Segmentarr Output Profile exists

[ ] M3U or HDHR is using the Segmentarr Output Profile

[ ] M3U URL contains the current output_profile ID, when using M3U

[ ] M3U URL points to the Segmentarr Output Profile ID (initial setup; normally unchanged afterward)

[ ] Channel was restarted after the profile change

[ ] Segmentarr supervisor shows the selected audio mode

[ ] Segmentarr FFmpeg shows the expected audio encoder

[ ] Segmentarr Output Profile uses -c:a copy

[ ] Downstream player reports the selected audio codec
```

---

# The Key Rule

**Do not use Dispatcharr's upstream audio statistic by itself to determine whether Segmentarr's audio conversion worked.**

Follow the complete chain:

```
Segmentarr setting
       |
       v
Actions -> Apply & Synchronize
       |
       v
Segmentarr-generated Output Profile
       |
       v
M3U / HDHR uses that Output Profile
       |
       v
Segmentarr supervisor shows selected audio
       |
       v
Segmentarr FFmpeg shows selected encoder
       |
       v
Dispatcharr Output Profile copies audio
       |
       v
Actual player reports selected codec
```

If the provider sends AC3, Segmentarr is set to MP3, the Segmentarr finalizer shows `libmp3lame`, the Dispatcharr Output Profile uses `-c:a copy`, and the downstream player reports MP3, then Segmentarr is working correctly even if Dispatcharr's upstream statistics still show AC3.

## Logs

Each stream writes to a persistent log that survives plugin updates:

```sh
docker exec dispatcharr tail -n 200 /data/segmentarr/logs/segmentarr.log
```

The file rotates at 5 MB to `segmentarr.log.1`. Lines carry a date, time, supervisor pid, and channel tag (`ch=<stream id>`). FFmpeg and CVLC output from the pipeline is captured in the same log.

| Line | Meaning |
|---|---|
| `segment late: 9.1s (typical 2.0s)` | The provider delivered a segment much later than usual. |
| `downstream slow: write blocked 7.4s` | Dispatcharr, CVLC, or the client stopped reading. |
| `hb 60s: segments=... max_gap=... max_write_block=... max_queue=...` | One-minute health summary. |
| `timeline break (...)` / `skipped to live` / `ingest exited` | A timestamp break was healed, backlog was dropped, or the provider connection was restarted. |

## Tradeoffs

- Adds roughly one segment plus a keyframe wait of latency, plus the CVLC cache when enabled.
- RAM use is small: 30 seconds of an 8 Mbps stream is about 30 MB.

## Files

| File | Purpose |
|---|---|
| `plugin.py` | Dispatcharr plugin: settings and profile creation |
| `segmentarr-supervisor.py` | Runs the FFmpeg stages and CVLC, plus timeline healing |
| `plugin.json` | Plugin metadata |

Releases are built by tagging `vX.Y.Z`; the workflow checks that the tag, `plugin.py`, and `plugin.json` all carry the same version. Current development version: 1.5.6.

## License

MIT
