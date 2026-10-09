"""Segmentarr Dispatcharr plugin.

HLS-segmenting stream profile + matching native Output Profile.

    provider (XC / URL) -> ffmpeg HLS segmenter -> timeline healer -> ffmpeg finalizer
        -> Dispatcharr matching Output Profile -> live MPEG-TS
"""

from __future__ import annotations

import shlex
from pathlib import Path

from apps.accounts.models import User
from apps.plugins.models import PluginConfig
from core.models import CoreSettings, OutputProfile, StreamProfile

SUPERVISOR = "segmentarr-supervisor.py"

# Profile key -> display label
PROFILES = {
    "standard": "Standard (2s segments)",
    "lowlatency": "Low Latency (1s segments)",
    "resilient": "Resilient (4s segments)",
}

AUDIO = {
    "aac": ("AAC", "-c:a aac -b:a 192k -ac 2"),
    "ac3": ("AC3", "-c:a ac3 -b:a 192k -ac 2"),
    "eac3": ("E-AC3", "-c:a eac3 -b:a 192k -ac 2"),
    "opus": ("Opus", "-c:a libopus -b:a 128k -ac 2"),
    "mp3": ("MP3", "-c:a libmp3lame -b:a 192k -ac 2"),
    "copy": ("Copy", "-c:a copy"),
}

# Setting id -> (environment variable, default, [(value, label)], field label, description)
TUNING = {
    "cvlc_cache": ("SEGMENTARR_CVLC", "1000",
                   [("0", "Off (no cvlc)"), ("300", "300 ms"), ("1000", "1000 ms"), ("3000", "3000 ms"), ("5000", "5000 ms")],
                   "CVLC Network Cache",
                   "Puts cvlc as the last stage before Dispatcharr (like Profilarr) with this caching. Off removes cvlc."),
    "stall_timeout": ("SEGMENTARR_STALL", "25", [("10", "10s"), ("15", "15s"), ("25", "25s"), ("40", "40s")],
                      "Stall Timeout",
                      "Restart the provider connection if no new segment appears for this long."),
    "max_catchup": ("SEGMENTARR_CATCHUP", "20", [("10", "10s"), ("20", "20s"), ("40", "40s"), ("60", "60s")],
                    "Max Catch-up Backlog",
                    "Queued media beyond Buffer + this value is dropped to jump back to live."),
    "gap_tolerance": ("SEGMENTARR_GAP", "1", [("0.5", "0.5s"), ("1", "1s"), ("2", "2s"), ("5", "5s")],
                      "Timeline Gap Tolerance",
                      "Forward PCR jumps up to this size are kept as-is; larger breaks are stitched shut."),
    "io_timeout": ("SEGMENTARR_IOTIMEOUT", "15", [("10", "10s"), ("15", "15s"), ("30", "30s")],
                   "Provider I/O Timeout",
                   "Treat the provider connection as dead after this long without data."),
    "probe_seconds": ("SEGMENTARR_PROBE", "3", [("1", "1s"), ("3", "3s"), ("5", "5s")],
                      "Stream Probe Time",
                      "How much stream ffmpeg analyses before starting. Lower tunes faster; higher detects odd streams better."),
}

ENV_FLAGS = {
    "SEGMENTARR_CVLC": "--cvlc",
    "SEGMENTARR_STALL": "--stall",
    "SEGMENTARR_CATCHUP": "--catchup",
    "SEGMENTARR_GAP": "--gap",
    "SEGMENTARR_IOTIMEOUT": "--iotimeout",
    "SEGMENTARR_PROBE": "--probe",
}

STREAM_PREFIXES = ("Segmentarr Profile -", "Segarr |")
OUTPUT_PREFIXES = ("Segmentarr Output -", "SegOut |")


class Plugin:
    name = "Segmentarr"
    version = "1.5.5"
    description = "HLS-segmenting stream profile for Dispatcharr: splits XC/URL provider streams into segments, repairs timestamp breaks, applies the selected audio mode, and pipes clean MPEG-TS to a matching Output Profile."
    author = "Tw1zT3d2four7"
    help_url = "https://github.com/Tw1zT3d2four7/Segmentarr"

    plugin_dir = Path(__file__).resolve().parent
    plugin_key = plugin_dir.name.replace(" ", "_").lower()

    def __init__(self):
        try:
            self.context = PluginConfig.objects.get(key=self.plugin_key)
            self.settings = self.context.settings or {}
        except PluginConfig.DoesNotExist:
            self.context = None
            self.settings = {}
        self.fields = [
            {
                "id": "segment_profile", "label": "Segment Profile", "type": "select",
                "default": "standard",
                "options": [{"value": k, "label": v} for k, v in PROFILES.items()],
            },
            *[
                {
                    "id": sid, "label": label, "type": "select", "default": default, "description": desc,
                    "options": [{"value": v, "label": l} for v, l in opts],
                }
                for sid, (_env, default, opts, label, desc) in TUNING.items()
            ],
            {
                "id": "audio_override", "label": "Audio Transcoding Override", "type": "select",
                "default": "aac",
                "options": [{"value": k, "label": v[0]} for k, v in AUDIO.items()],
            },
            {
                "id": "output_profile_id", "label": "Preferred Output Profile ID (optional)", "type": "text",
                "default": "",
                "description": "Pin Segmentarr to a specific Dispatcharr Output Profile ID. If that ID is missing, Segmentarr can recreate it. Do not enter an ID belonging to another profile.",
            },
        ]
        self.actions = [
            {
                "id": "generate_profile", "label": "Apply & Synchronize",
                "button_label": "Apply & Synchronize", "button_color": "green",
                "description": "Create or update the selected Segmentarr Stream Profile and matching Output Profile, and make both the defaults.",
            }
        ]

    def _choice(self, setting_id, default, options, label_of):
        """Resolve a select setting; unset, null, or stale values fall back to the default."""
        raw = self.settings.get(setting_id)
        if isinstance(raw, dict):
            raw = raw.get("value", raw.get("id"))
        raw = str(raw).strip() if raw is not None else ""
        if raw in options:
            return raw
        for key, val in options.items():
            if raw.lower() in (key.lower(), str(label_of(val)).lower()):
                return key
        return default

    def _tuning_flags(self):
        parts = []
        for sid, (env, default, opts, _l, _d) in TUNING.items():
            val = str(self.settings.get(sid, default))
            if val not in {v for v, _ in opts}:
                val = default
            parts.append(f"{ENV_FLAGS[env]} {val}")
        return " ".join(parts)

    @staticmethod
    def _output_parameters(audio):
        # I already produce the selected audio codec in Segmentarr's finalizer.
        # The matching Dispatcharr Output Profile must preserve that codec;
        # re-encoding here would double-transcode (for example, AC3 -> MP3 -> AAC).
        return (
            "-stats -fflags +discardcorrupt+genpts+nobuffer "
            "-probesize 512K -analyzeduration 0 "
            "-i pipe:0 -map 0 -c:v copy -c:a copy "
            "-max_muxing_queue_size 4096 -flush_packets 1 "
            "-mpegts_flags +pat_pmt_at_frames+resend_headers+initial_discontinuity "
            "-f mpegts pipe:1"
        )

    def _generate_profile(self):
        seg_key = self._choice("segment_profile", "standard", PROFILES, lambda v: v)
        audio = self._choice("audio_override", "aac", AUDIO, lambda v: v[0])

        seg_short = {
            "standard": "Std2s",
            "lowlatency": "LL1s",
            "resilient": "Res4s",
        }[seg_key]
        cv = str(self.settings.get("cvlc_cache", "1000"))
        cv = cv if cv in {v for v, _ in TUNING["cvlc_cache"][2]} else "1000"
        cv_short = {
            "0": "Off",
            "300": "300ms",
            "1000": "1s",
            "3000": "3s",
            "5000": "5s",
        }[cv]

        # I keep these names compact because Dispatcharr displays the active
        # stream/profile name in a narrow UI column.
        suffix = f"{seg_short} | CV{cv_short} | {AUDIO[audio][0]}"
        stream_target = f"Segarr | {suffix}"
        output_target = f"SegOut | {suffix}"
        command = "python3"
        stream_parameters = (
            f"{shlex.quote(str(self.plugin_dir / SUPERVISOR))} {self._tuning_flags()} {seg_key} {audio} "
            "'{userAgent}' '{streamUrl}'"
        )
        # I keep the selected audio mode identical in both the Stream Profile
        # and matching Output Profile so either stage applies the same codec.
        output_parameters = self._output_parameters(audio)

        # I reuse the existing Segmentarr profiles so Dispatcharr's assigned IDs stay stable.
        # The Output Profile currently assigned to the main user takes priority, because client
        # M3U URLs may explicitly reference that ID.
        stream_candidates = [
            p for p in StreamProfile.objects.all()
            if not p.locked and any(p.name.startswith(prefix) for prefix in STREAM_PREFIXES)
        ]
        output_candidates = [
            p for p in OutputProfile.objects.all()
            if not p.locked and any(p.name.startswith(prefix) for prefix in OUTPUT_PREFIXES)
        ]
        try:
            user = User.objects.get(id=1)
        except User.DoesNotExist:
            user = None

        # An explicit ID is useful when a previous plugin version deleted the profile
        # and an existing client URL still pins the old Dispatcharr ID.
        raw_preferred_id = self.settings.get("output_profile_id", "")
        if isinstance(raw_preferred_id, dict):
            raw_preferred_id = raw_preferred_id.get("value", raw_preferred_id.get("id", ""))
        raw_preferred_id = str(raw_preferred_id).strip() if raw_preferred_id is not None else ""
        preferred_id = None
        if raw_preferred_id:
            try:
                preferred_id = int(raw_preferred_id)
                if preferred_id < 1:
                    raise ValueError
            except (TypeError, ValueError):
                return {"status": "error", "message": "Preferred Output Profile ID must be a positive integer or blank."}

            output_profile = OutputProfile.objects.filter(id=preferred_id).first()
            if output_profile is not None:
                if output_profile.locked or not any(output_profile.name.startswith(prefix) for prefix in OUTPUT_PREFIXES):
                    return {
                        "status": "error",
                        "message": f"Output Profile ID {preferred_id} already belongs to a non-Segmentarr or locked profile; refusing to overwrite it.",
                    }
        else:
            user_output_id = (user.custom_properties or {}).get("output_profile") if user else None
            output_profile = next(
                (p for p in output_candidates if str(p.id) == str(user_output_id)),
                None,
            )
            if output_profile is None:
                output_profile = next((p for p in output_candidates if p.name == output_target), None)
            if output_profile is None and output_candidates:
                output_profile = sorted(output_candidates, key=lambda p: p.id)[0]

        stream_profile = next((p for p in stream_candidates if p.name == stream_target), None)
        if stream_profile is None and stream_candidates:
            stream_profile = sorted(stream_candidates, key=lambda p: p.id)[0]

        try:
            if stream_profile is None:
                stream_profile = StreamProfile(name=stream_target, locked=False)
            stream_profile.name = stream_target
            stream_profile.command = command
            stream_profile.parameters = stream_parameters
            stream_profile.is_active = True
            stream_profile.save()
        except Exception as e:
            return {"status": "error", "message": f"Could not create Stream Profile: {type(e).__name__}: {e}"}

        try:
            if output_profile is None:
                if preferred_id is not None:
                    # If a newer generated profile has taken the desired name, keep its
                    # ID and record but move its label aside before restoring the pinned ID.
                    # This also avoids a name-uniqueness conflict on Dispatcharr versions
                    # where OutputProfile.name is unique.
                    conflicting = OutputProfile.objects.filter(name=output_target).exclude(id=preferred_id).first()
                    if conflicting is not None:
                        if conflicting.locked or not any(conflicting.name.startswith(prefix) for prefix in OUTPUT_PREFIXES):
                            return {
                                "status": "error",
                                "message": f"Cannot restore Output Profile ID {preferred_id}: the target name is already used by a non-Segmentarr or locked profile (ID {conflicting.id}).",
                            }
                        conflicting.name = f"{output_target} (preserved ID {conflicting.id})"
                        conflicting.save(update_fields=["name"])
                    # Recreate the missing profile at the user-pinned ID so existing
                    # output_profile=<ID> M3U URLs continue to resolve.
                    output_profile = OutputProfile(id=preferred_id, name=output_target, locked=False)
                else:
                    output_profile = OutputProfile(name=output_target, locked=False)
            output_profile.name = output_target
            output_profile.command = "ffmpeg"
            output_profile.parameters = output_parameters
            output_profile.is_active = True
            output_profile.save()
        except Exception as e:
            return {"status": "error", "message": f"Could not create Output Profile: {type(e).__name__}: {e}"}

        # Do not automatically delete other generated profiles. Existing channels or
        # client M3U URLs may still reference their IDs. Reuse the selected records in
        # place; leave any legacy duplicates intact rather than silently breaking those
        # assignments. Cleanup can be handled separately after references are verified.

        try:
            CoreSettings._update_group(
                "stream_settings", "Stream Settings",
                {"default_stream_profile": stream_profile.id, "hdhr_output_profile_id": output_profile.id},
            )
        except Exception as e:
            return {"status": "error", "message": f"Profiles synchronized, but defaults could not be changed: {type(e).__name__}: {e}"}

        try:
            if user is None:
                raise User.DoesNotExist("Dispatcharr user id=1 was not found")
            props = dict(user.custom_properties or {})
            props["output_profile"] = output_profile.id
            user.custom_properties = props
            user.save(update_fields=["custom_properties"])
        except Exception as e:
            return {"status": "error", "message": f"Profiles synchronized, but live Output Profile default could not be changed: {type(e).__name__}: {e}"}

        return {
            "status": "ok",
            "message": f"Segmentarr synchronized: {stream_target} | {output_target} | "
                       f"Stream Default: {stream_profile.id} | Output Default: {output_profile.id}",
        }

    def stop(self, context):
        """Running streams are deliberately left alone so reloading or updating the plugin never interrupts playback."""
        return None

    def run(self, action, params, context):
        self.settings = context.get("settings", {}) or {}
        if action == "generate_profile":
            return self._generate_profile()
        return {"status": "error", "message": f"Unknown action: {action}"}
