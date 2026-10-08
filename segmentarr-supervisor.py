#!/usr/bin/env python3
"""Segmentarr supervisor (Dispatcharr stream profile engine).

    provider (XC / plain URL)
        |  ffmpeg ingest: -c copy -f hls  -> keyframe-aligned .ts segments in tmpfs
        v
    this script: validate, resync, drop TEI packets, stitch PCR/PTS/DTS onto one
        continuous clock whenever a segment (or a point inside one) breaks the timeline
        |  stdin
        v
    ffmpeg finalizer: -i pipe:0 -c copy ... pipe:1  ->  stdout  ->  Dispatcharr

Usage (matches Dispatcharr's '{userAgent}' '{streamUrl}' parameter order):
    segmentarr-supervisor.py <profile> <user_agent> <stream_url>

stdout carries ONLY MPEG-TS from the finalizer. Everything else logs to stderr.
"""
from __future__ import annotations

import collections
import contextlib
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

PKT = 188
SYNC = 0x47
PCR_MOD = (1 << 33) * 300  # 27 MHz units
TS_MOD = 1 << 33  # 90 kHz units
NULL_PKT = b"\x47\x1f\xff\x10" + b"\xff" * 184
NO_PES_HEADER = {0xBC, 0xBE, 0xBF, 0xF0, 0xF1, 0xF2, 0xF8, 0xFF}

PRESETS = {
    "standard": {"seg": 2, "start": 1, "audio": "copy"},
    "lowlatency": {"seg": 1, "start": 1, "audio": "copy"},
    "resilient": {"seg": 4, "start": 2, "audio": "copy"},
}
COMMON = {
    "backlog_seconds": 20,  # more than this queued => skip ahead to live
    "stall_seconds": 25,  # no new segment for this long => restart ingest
    "gap_max_seconds": 1.0,  # PCR forward gap kept as-is up to this; beyond it is stitched shut
    "probe_us": 3_000_000,
    "rw_timeout_us": 15_000_000,
    "reconnect_delay_max": 5,  # seconds, provider reconnect backoff ceiling
    "cvlc_cache": 0,  # ms; >0 puts cvlc (with this caching) as the last stage before stdout
    "max_fast_failures": 8,  # consecutive ingest/finalizer restarts without progress => exit 1
}

# env var -> (cfg key, converter). Set by the plugin's wrapper scripts from its settings.
ENV_TUNING = {
    "SEGMENTARR_STALL": ("stall_seconds", float),
    "SEGMENTARR_CATCHUP": ("backlog_seconds", float),
    "SEGMENTARR_GAP": ("gap_max_seconds", float),
    "SEGMENTARR_RECONNECT": ("reconnect_delay_max", int),
    "SEGMENTARR_IOTIMEOUT": ("rw_timeout_us", lambda v: int(float(v) * 1_000_000)),
    "SEGMENTARR_PROBE": ("probe_us", lambda v: int(float(v) * 1_000_000)),
    "SEGMENTARR_CVLC": ("cvlc_cache", int),
}

# CLI flag -> env var name, so settings can ride in the stream profile's Parameters (no wrapper scripts).
FLAG_ENV = {
    "--cvlc": "SEGMENTARR_CVLC",
    "--stall": "SEGMENTARR_STALL",
    "--catchup": "SEGMENTARR_CATCHUP",
    "--gap": "SEGMENTARR_GAP",
    "--reconnect": "SEGMENTARR_RECONNECT",
    "--iotimeout": "SEGMENTARR_IOTIMEOUT",
    "--probe": "SEGMENTARR_PROBE",
}


def parse_args(argv: list[str]) -> tuple[dict[str, str], list[str]]:
    flags: dict[str, str] = {}
    pos: list[str] = []
    i = 0
    while i < len(argv):
        if argv[i] in FLAG_ENV and i + 1 < len(argv):
            flags[FLAG_ENV[argv[i]]] = argv[i + 1]
            i += 2
        else:
            pos.append(argv[i])
            i += 1
    return flags, pos


STOP = False
CHILDREN: list[subprocess.Popen] = []


LOG_FH = None  # persistent log file (survives plugin updates; /data is the Dispatcharr volume)
LOG_TAG = ""


def open_logfile():
    for d in (os.environ.get("SEGMENTARR_LOGDIR"), "/data/segmentarr/logs", "/tmp/segmentarr-logs"):
        if not d:
            continue
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            p = Path(d) / "segmentarr.log"
            if p.exists() and p.stat().st_size > 5 * 1024 * 1024:
                os.replace(p, Path(d) / "segmentarr.log.1")
            return open(p, "ab", buffering=0)
        except OSError:
            continue
    return None


def log(msg: str, file_only: bool = False) -> None:
    ts = time.strftime("%H:%M:%S")
    if not file_only:
        print(f"[segmentarr] {ts} {msg}", file=sys.stderr, flush=True)
    if LOG_FH is not None:
        with contextlib.suppress(OSError, ValueError):
            LOG_FH.write(f"{time.strftime('%Y-%m-%d')} {ts} [{os.getpid()} {LOG_TAG}] {msg}\n".encode())


def _pdeathsig() -> None:
    # Children die with us even if Dispatcharr SIGKILLs the supervisor.
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass


# --------------------------------------------------------------------------- timeline stitcher


class Stitcher:
    """Rewrites PCR/PTS/DTS so the output timeline is continuous. Byte-identical when healthy."""

    def __init__(self, gap_max_s: float) -> None:
        self.gap_max = int(gap_max_s * 27_000_000)
        self.pmt_pid: int | None = None
        self.pcr_pid: int | None = None
        self.es_pids: set[int] = set()
        self.offset = 0  # 27 MHz units
        self.last_in: int | None = None
        self.last_out: int | None = None
        self.interval = 27_000_000 // 25  # nominal PCR spacing, refined from healthy deltas
        self.stitches = 0
        self.tei_dropped = 0
        self.resync_bytes = 0

    # ---- helpers
    @staticmethod
    def _sdiff(a: int, b: int) -> int:
        d = (a - b) % PCR_MOD
        return d - PCR_MOD if d > PCR_MOD // 2 else d

    def _align(self, data: bytes) -> bytes:
        n = len(data)
        if n >= PKT and n % PKT == 0 and data[0:n:PKT].count(SYNC) == n // PKT:
            return data
        parts = []
        pos = 0
        while pos + PKT <= n:
            if data[pos] == SYNC and (pos + PKT == n or data[pos + PKT] == SYNC):
                parts.append(data[pos : pos + PKT])
                pos += PKT
            else:
                nxt = data.find(b"\x47", pos + 1)
                if nxt < 0:
                    self.resync_bytes += n - pos
                    break
                self.resync_bytes += nxt - pos
                pos = nxt
        return b"".join(parts)

    def _parse_pat(self, d: bytes, o: int) -> None:
        p = o + 5 + d[o + 4]  # pointer_field
        if d[p] != 0x00:
            return
        sec_len = ((d[p + 1] & 0x0F) << 8) | d[p + 2]
        end = min(p + 3 + sec_len - 4, o + PKT)
        i = p + 8
        while i + 4 <= end:
            prog = (d[i] << 8) | d[i + 1]
            pid = ((d[i + 2] & 0x1F) << 8) | d[i + 3]
            if prog != 0:
                self.pmt_pid = pid
                return
            i += 4

    def _parse_pmt(self, d: bytes, o: int) -> None:
        p = o + 5 + d[o + 4]
        if d[p] != 0x02:
            return
        sec_len = ((d[p + 1] & 0x0F) << 8) | d[p + 2]
        end = min(p + 3 + sec_len - 4, o + PKT)
        self.pcr_pid = ((d[p + 8] & 0x1F) << 8) | d[p + 9]
        info_len = ((d[p + 10] & 0x0F) << 8) | d[p + 11]
        i = p + 12 + info_len
        pids = set()
        while i + 5 <= end:
            pids.add(((d[i + 1] & 0x1F) << 8) | d[i + 2])
            i += 5 + (((d[i + 3] & 0x0F) << 8) | d[i + 4])
        if pids:
            self.es_pids = pids

    def _on_pcr(self, pcr_in: int) -> int:
        if self.last_in is None:
            self.last_in = self.last_out = pcr_in
            return pcr_in
        delta = self._sdiff(pcr_in, self.last_in)
        if 0 <= delta <= self.gap_max:
            if 0 < delta < 5_400_000:  # 200 ms: refine nominal interval
                self.interval = int(self.interval * 0.9 + delta * 0.1)
        else:
            target = (self.last_out + self.interval) % PCR_MOD
            self.offset = (target - pcr_in) % PCR_MOD
            self.stitches += 1
            log(f"timeline break ({delta / 27e6:+.2f}s) -> stitched, offset={self.offset / 27e6:.2f}s")
        out = (pcr_in + self.offset) % PCR_MOD
        self.last_in, self.last_out = pcr_in, out
        return out

    @staticmethod
    def _rd_ts(b: bytes, i: int) -> int:
        return (((b[i] >> 1) & 7) << 30) | (b[i + 1] << 22) | ((b[i + 2] >> 1) << 15) | (b[i + 3] << 7) | (b[i + 4] >> 1)

    @staticmethod
    def _wr_ts(b: bytearray, i: int, ts: int) -> None:
        b[i] = (b[i] & 0xF0) | (((ts >> 30) & 7) << 1) | 1
        b[i + 1] = (ts >> 22) & 0xFF
        b[i + 2] = (((ts >> 15) & 0x7F) << 1) | 1
        b[i + 3] = (ts >> 7) & 0xFF
        b[i + 4] = ((ts & 0x7F) << 1) | 1

    def _shift_pes(self, pkt: bytearray, p: int) -> None:
        if p + 14 > PKT or pkt[p] != 0 or pkt[p + 1] != 0 or pkt[p + 2] != 1:
            return
        if pkt[p + 3] in NO_PES_HEADER or (pkt[p + 6] & 0xC0) != 0x80:
            return
        flags = pkt[p + 7] >> 6
        off = (self.offset // 300) % TS_MOD
        if flags in (2, 3) and p + 14 <= PKT:
            self._wr_ts(pkt, p + 9, (self._rd_ts(pkt, p + 9) + off) % TS_MOD)
        if flags == 3 and p + 19 <= PKT:
            self._wr_ts(pkt, p + 14, (self._rd_ts(pkt, p + 14) + off) % TS_MOD)

    # ---- main entry
    def process(self, data: bytes) -> bytes:
        data = self._align(data)
        out = []
        for o in range(0, len(data), PKT):
            b1 = data[o + 1]
            if b1 & 0x80:  # transport_error_indicator -> null packet keeps pacing, drops garbage
                out.append(NULL_PKT)
                self.tei_dropped += 1
                continue
            pid = ((b1 & 0x1F) << 8) | data[o + 2]
            pusi = b1 & 0x40
            afc = (data[o + 3] >> 4) & 3
            if afc == 0:
                out.append(data[o : o + PKT])
                continue
            try:
                if pid == 0 and pusi and afc & 1:
                    self._parse_pat(data, o)
                elif pid == self.pmt_pid and pusi and afc & 1:
                    self._parse_pmt(data, o)
            except IndexError:
                pass
            has_pcr = (
                afc & 2
                and data[o + 4] >= 7
                and data[o + 5] & 0x10
                and (self.pcr_pid is None or pid == self.pcr_pid)
            )
            needs_pes = pusi and afc & 1 and self.offset and pid in self.es_pids
            if not has_pcr and not needs_pes:
                out.append(data[o : o + PKT])
                continue
            pkt = bytearray(data[o : o + PKT])
            if has_pcr:
                base = (pkt[6] << 25) | (pkt[7] << 17) | (pkt[8] << 9) | (pkt[9] << 1) | (pkt[10] >> 7)
                ext = ((pkt[10] & 1) << 8) | pkt[11]
                new = self._on_pcr(base * 300 + ext)
                if self.offset:
                    nb, ne = divmod(new, 300)
                    pkt[6], pkt[7], pkt[8], pkt[9] = (nb >> 25) & 0xFF, (nb >> 17) & 0xFF, (nb >> 9) & 0xFF, (nb >> 1) & 0xFF
                    pkt[10] = ((nb & 1) << 7) | 0x7E | ((ne >> 8) & 1)
                    pkt[11] = ne & 0xFF
            if pusi and afc & 1 and self.offset and pid in self.es_pids:
                p = 4 + (1 + pkt[4] if afc & 2 else 0)
                if p < PKT:
                    self._shift_pes(pkt, p)
            out.append(bytes(pkt))
        return b"".join(out)


# --------------------------------------------------------------------------- process builders


def ingest_cmd(cfg: dict, ua: str, url: str, gen: int, wd: Path) -> list[str]:
    c = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-nostdin"]
    if ua:
        c += ["-user_agent", ua]
    if url.startswith(("http://", "https://")):
        c += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_at_eof", "1", "-reconnect_delay_max", str(cfg["reconnect_delay_max"])]
    c += [
        "-rw_timeout", str(cfg["rw_timeout_us"]),
        "-analyzeduration", str(cfg["probe_us"]), "-probesize", str(cfg["probe_us"]),
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        "-i", url,
        "-map", "0:v:0?", "-map", "0:a?", "-c", "copy",
        "-f", "hls", "-hls_time", str(cfg["seg"]), "-hls_list_size", "3",
        "-hls_flags", "independent_segments+temp_file+omit_endlist",
        "-hls_segment_type", "mpegts", "-start_number", "0",
        "-hls_segment_filename", str(wd / f"seg_{gen:04d}_%08d.ts"),
        str(wd / f"index_{gen:04d}.m3u8"),
    ]
    return c


def finalizer_cmd(cfg: dict) -> list[str]:
    c = [
        "ffmpeg", "-hide_banner", "-loglevel", "info", "-stats", "-nostdin",
        "-fflags", "+genpts+discardcorrupt", "-err_detect", "ignore_err",
        "-f", "mpegts", "-i", "pipe:0",
        "-map", "0:v:0?", "-map", "0:a?", "-c:v", "copy",
    ]
    if cfg["audio"] == "copy":
        c += ["-c:a", "copy"]
    else:
        c += ["-c:a", "aac", "-b:a", "128k", "-ac", "2", "-af", "aresample=async=1:first_pts=0"]
    c += [
        "-avoid_negative_ts", "make_zero",
        "-f", "mpegts", "-mpegts_flags", "+resend_headers+pat_pmt_at_frames+initial_discontinuity",
        "-flush_packets", "1", "pipe:1",
    ]
    return c


def cvlc_cmd(cache_ms: int) -> list[str]:
    # Same flags as Profilarr's cvlc tail, plus vlc://quit so cvlc exits when its input ends.
    return [
        "cvlc", "-I", "dummy", "--no-lua", "--no-auto-preparse", "--no-dbus", "--no-interact", "--no-stats",
        "--aout", "adummy", "--vout", "vdummy", "--no-sout-all", "--sout-keep",
        "--network-caching", str(int(cache_ms)), "--sout-mux-caching", "1500",
        "--adaptive-logic=highest", "--sout=#std{access=file,mux=ts,dst=-}",
        "fd://0", "vlc://quit",
    ]


def spawn(cmd: list[str], **kw) -> subprocess.Popen:
    p = subprocess.Popen(cmd, preexec_fn=_pdeathsig, **kw)
    CHILDREN.append(p)
    return p


def kill(p: subprocess.Popen | None) -> None:
    if p is None or p.poll() is not None:
        return
    p.terminate()
    try:
        p.wait(2)
    except subprocess.TimeoutExpired:
        p.kill()


# --------------------------------------------------------------------------- workdir


def pick_base() -> Path:
    for cand in (os.environ.get("SEGMENTARR_TMP"), "/dev/shm", "/tmp"):
        if not cand:
            continue
        try:
            if shutil.disk_usage(cand).free > 256 * 1024 * 1024:
                return Path(cand) / "segmentarr"
        except OSError:
            continue
    return Path("/tmp/segmentarr")


def reap_stale(base: Path) -> None:
    if not base.is_dir():
        return
    for d in base.iterdir():
        if d.name.isdigit():
            try:
                os.kill(int(d.name), 0)
            except ProcessLookupError:
                shutil.rmtree(d, ignore_errors=True)
            except PermissionError:
                pass


SEG_RE = re.compile(r"seg_(\d+)_(\d+)\.ts$")


def list_segments(wd: Path) -> list[tuple[int, int, Path]]:
    out = []
    for p in wd.iterdir():
        m = SEG_RE.match(p.name)
        if m:
            out.append((int(m.group(1)), int(m.group(2)), p))
    out.sort()
    return out


# --------------------------------------------------------------------------- main


def _sig(_s, _f) -> None:
    global STOP
    STOP = True


def main() -> int:
    cli_flags, positional = parse_args(sys.argv[1:])
    if len(positional) < 3:
        log("usage: segmentarr-supervisor.py [--cvlc MS --stall S ...] <profile> <user_agent> <stream_url>")
        return 2
    profile, ua, url = positional[0], positional[1], positional[2]
    global LOG_FH, LOG_TAG
    LOG_FH = open_logfile()
    with contextlib.suppress(Exception):
        LOG_TAG = "ch=" + (urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].split(".")[0] or "?")[:24]
    cfg = {**COMMON, **PRESETS.get(profile, PRESETS["standard"])}
    for env, (key, conv) in ENV_TUNING.items():
        raw = cli_flags.get(env, os.environ.get(env))
        if raw not in (None, ""):
            try:
                cfg[key] = conv(raw)
            except ValueError:
                log(f"ignoring bad {env}={raw!r}")
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGPIPE, signal.SIG_IGN)  # broken pipes surface as BrokenPipeError, not silent death

    base = pick_base()
    base.mkdir(parents=True, exist_ok=True)
    reap_stale(base)
    wd = base / str(os.getpid())
    wd.mkdir(parents=True, exist_ok=True)
    run_dir = Path(os.environ.get("SEGMENTARR_RUN", "/data/plugins/segmentarr/run"))
    pidfile = run_dir / f"{os.getpid()}.pid"
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        pidfile.write_text(str(os.getpid()))
    except OSError:
        pidfile = None

    stitcher = Stitcher(cfg["gap_max_seconds"])
    max_backlog = max(3, math.ceil(cfg["backlog_seconds"] / cfg["seg"]))
    cvlc: subprocess.Popen | None = None
    pipe_w: int | None = None  # write end feeding cvlc; held open here so a finalizer restart never EOFs cvlc

    def start_cvlc() -> None:
        nonlocal cvlc, pipe_w
        if pipe_w is not None:
            with contextlib.suppress(OSError):
                os.close(pipe_w)
        r, w = os.pipe()
        env = {**os.environ, "HOME": str(wd), "XDG_CONFIG_HOME": str(wd), "XDG_CACHE_HOME": str(wd)}
        cvlc = spawn(cvlc_cmd(cfg["cvlc_cache"]), stdin=r, stdout=sys.stdout.fileno(), stderr=LOG_FH, env=env)
        os.close(r)
        pipe_w = w
    gen = -1
    prev_gen = -1
    prev_mtime = 0.0
    gap_hist: collections.deque = collections.deque(maxlen=10)
    hb_every = float(os.environ.get("SEGMENTARR_HEARTBEAT", "60"))
    win_start = time.monotonic()
    win_segs = 0
    win_max_gap = 0.0
    win_max_block = 0.0
    win_max_queue = 0
    ingest: subprocess.Popen | None = None
    fin: subprocess.Popen | None = None
    failures = 0
    last_seg_time = time.monotonic()
    started = False
    delivered = 0
    rc = 0
    log(
        f"profile={profile} seg={cfg['seg']}s "
        f"stall={cfg['stall_seconds']:g}s catchup={cfg['backlog_seconds']:g}s gap={cfg['gap_max_seconds']:g}s "
        f"reconnect<={cfg['reconnect_delay_max']}s io={cfg['rw_timeout_us'] / 1e6:g}s probe={cfg['probe_us'] / 1e6:g}s "
        f"cvlc={'off' if not cfg['cvlc_cache'] else str(cfg['cvlc_cache']) + 'ms'} audio={cfg['audio']}"
    )

    try:
        while not STOP:
            if time.monotonic() - win_start >= hb_every:
                log(
                    f"hb {hb_every:g}s: segments={win_segs} max_gap={win_max_gap:.1f}s "
                    f"max_write_block={win_max_block:.1f}s max_queue={win_max_queue}",
                    file_only=True,
                )
                win_start = time.monotonic()
                win_segs, win_max_gap, win_max_block, win_max_queue = 0, 0.0, 0.0, 0
            # --- ingest supervision
            if ingest is None or ingest.poll() is not None:
                if ingest is not None:
                    log(f"ingest exited rc={ingest.returncode}; restarting")
                    failures += 1
                    if failures > cfg["max_fast_failures"]:
                        log("ingest failing repeatedly; giving up")
                        rc = 1
                        break
                    time.sleep(min(5, failures))
                gen += 1
                ingest = spawn(ingest_cmd(cfg, ua, url, gen, wd), stdout=subprocess.DEVNULL, stderr=LOG_FH)
                last_seg_time = time.monotonic()

            segs = list_segments(wd)
            if not segs:
                if time.monotonic() - last_seg_time > cfg["stall_seconds"]:
                    log("no segments (stalled); restarting ingest")
                    kill(ingest)
                time.sleep(0.1)
                continue
            if not started:
                if len(segs) < cfg["start"]:
                    time.sleep(0.1)
                    continue
                started = True

            # --- fell behind (slow downstream): jump to live, timeline stitcher closes the gap
            if len(segs) > max_backlog:
                for _, _, p in segs[: -2]:
                    p.unlink(missing_ok=True)
                log(f"backlog {len(segs)} segments; skipped to live")
                segs = segs[-2:]

            g, s, path = segs[0]
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            path.unlink(missing_ok=True)
            last_seg_time = time.monotonic()
            if mtime and g == prev_gen and prev_mtime:
                gap = mtime - prev_mtime
                base = statistics.median(gap_hist) if len(gap_hist) >= 3 else float(cfg["seg"])
                if gap > max(base * 1.5, base + 1.5):
                    log(f"segment late: {gap:.1f}s (typical {base:.1f}s) gen {g} seg {s}")
                gap_hist.append(gap)
                win_max_gap = max(win_max_gap, gap)
            win_segs += 1
            win_max_queue = max(win_max_queue, len(segs))
            if g != prev_gen and prev_gen >= 0:
                log(f"first segment of ingest gen {g} delivered")
            prev_gen, prev_mtime = g, mtime
            healed = stitcher.process(data)
            if not healed:
                continue

            # --- cvlc tail supervision
            if cfg["cvlc_cache"] > 0 and (cvlc is None or cvlc.poll() is not None):
                if cvlc is not None:
                    if cvlc.returncode == -signal.SIGPIPE:
                        log("downstream closed the pipe; exiting")
                        break
                    log(f"cvlc exited rc={cvlc.returncode}; restarting")
                    failures += 1
                    if failures > cfg["max_fast_failures"]:
                        rc = 1
                        break
                    kill(fin)
                    fin = None
                start_cvlc()

            # --- finalizer supervision + delivery
            if fin is None or fin.poll() is not None:
                if fin is not None:
                    log(f"finalizer exited rc={fin.returncode}; restarting")
                    failures += 1
                    if failures > cfg["max_fast_failures"]:
                        rc = 1
                        break
                fin = spawn(
                    finalizer_cmd(cfg), stdin=subprocess.PIPE, stderr=sys.stderr,
                    stdout=pipe_w if cfg["cvlc_cache"] > 0 else sys.stdout.fileno(),
                )
            try:
                t_w = time.monotonic()
                fin.stdin.write(healed)
                fin.stdin.flush()
                blocked = time.monotonic() - t_w
                win_max_block = max(win_max_block, blocked)
                if blocked > 0.75:
                    log(f"downstream slow: write blocked {blocked:.1f}s ({len(healed) // 1024} KB)")
                delivered += 1
                failures = 0
            except (BrokenPipeError, OSError):
                log("finalizer pipe closed")
                kill(fin)
    finally:
        if fin is not None and fin.stdin:
            try:
                fin.stdin.close()
            except OSError:
                pass
        kill(ingest)
        kill(fin)
        if pipe_w is not None:
            with contextlib.suppress(OSError):
                os.close(pipe_w)
            pipe_w = None
        if cvlc is not None:
            with contextlib.suppress(Exception):
                cvlc.wait(3)
        kill(cvlc)
        shutil.rmtree(wd, ignore_errors=True)
        if pidfile:
            pidfile.unlink(missing_ok=True)
        log(
            f"done: segments={delivered} stitches={stitcher.stitches} "
            f"tei_dropped={stitcher.tei_dropped} resync_bytes={stitcher.resync_bytes}"
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
