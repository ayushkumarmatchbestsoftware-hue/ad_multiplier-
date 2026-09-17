"""
Video prep — make any uploaded clip acceptable to the model it's sent to.

Users upload whatever their phone or editor produced: 480p screen recordings,
4K HDR iPhone footage, 120fps slow motion, WebM, 25-second ads. The video model
has an input contract (KLING["input"] in tools/ad_multiplier), and a clip
outside it fails on the provider with a bare "execution failed" — Kling O3 did
exactly that with a 478x850 upload. So before a video job is submitted the source
is probed and, only when something is out of range, re-encoded once to fit:

    resolution  scaled (Lanczos) into the engine's per-side range, aspect kept;
                padded only when the aspect is too extreme to fit both bounds
    frame rate  constant fps inside the engine's range
    duration    trimmed to the maximum, or held on the last frame up to the minimum
    format      H.264 High / yuv420p / AAC in MP4 with faststart, rotation baked in
    HDR         tone-mapped to SDR BT.709 so colours don't wash out
    size        bitrate-capped under the engine's MB limit

A clip already inside the contract is sent untouched. A converted clip is stored
next to the original in R2 and remembered on the media entry, keyed by what it was
converted to, so every variant in a batch — and every later edit of the same clip —
reuses one encode.

ffmpeg must never inherit the server's stdin/stdout: under stdio those are the
JSON-RPC channel.
"""

from __future__ import annotations

import asyncio
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import httpx

from config import R2_BUCKET_NAME, R2_UPLOAD_PREFIX
from tools import media_library as library
from tools.media_library import MediaError

ENCODE_TIMEOUT_SECONDS = 900
PROBE_TIMEOUT_SECONDS = 60

_HDR_TRANSFERS = ("smpte2084", "arib-std-b67")
_READY_CONTAINERS = ("mp4", "mov")
_READY_PIX_FMTS = ("yuv420p", "yuvj420p")
# Standard rates a measured average snaps to, so a phone's variable 29.87 fps
# becomes 29.97 rather than an odd constant rate.
_STANDARD_FPS = (24000 / 1001, 24.0, 25.0, 30000 / 1001, 30.0, 50.0, 60000 / 1001, 60.0)
_AUDIO_KBPS = 192


class VideoPrepError(MediaError):
    """The clip can't be made acceptable — the message says why, in user terms."""


# ── ffmpeg ────────────────────────────────────────────────────────────────────

@lru_cache(maxsize=1)
def ffmpeg_paths() -> tuple[str, str] | None:
    """(ffmpeg, ffprobe), or None when ffmpeg isn't installed.

    Claude Desktop starts the server with a thinner environment than a terminal,
    so common install locations are checked as well as PATH.
    """
    candidates: list[str] = []
    configured = os.getenv("FFMPEG_PATH", "").strip()
    if configured:
        candidates.append(str(Path(configured).parent if Path(configured).is_file() else Path(configured)))
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(str(Path(found).parent))
    local = os.getenv("LOCALAPPDATA", "")
    if local:
        candidates += glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*FFmpeg*", "*", "bin"))
        candidates.append(os.path.join(local, "Microsoft", "WinGet", "Links"))
    candidates += [r"C:\ProgramData\chocolatey\bin", r"C:\ffmpeg\bin", "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]

    exe = ".exe" if sys.platform == "win32" else ""
    for folder in candidates:
        ffmpeg, ffprobe = os.path.join(folder, "ffmpeg" + exe), os.path.join(folder, "ffprobe" + exe)
        if os.path.isfile(ffmpeg) and os.path.isfile(ffprobe):
            return ffmpeg, ffprobe
    return None


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        stdin=subprocess.DEVNULL,           # stdin is the MCP channel under stdio
        capture_output=True,                # so is stdout
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # no console flash from Claude Desktop
    )


# ── probe ─────────────────────────────────────────────────────────────────────

@dataclass
class Probe:
    width: int              # as displayed: rotation and pixel aspect applied
    height: int
    fps: float
    duration: float
    size_bytes: int
    container: str
    vcodec: str
    pix_fmt: str
    rotation: int
    square_pixels: bool
    has_audio: bool
    acodec: str
    color_transfer: str
    color_primaries: str
    color_space: str

    @property
    def hdr(self) -> bool:
        return self.color_transfer in _HDR_TRANSFERS


def _ratio(value: str) -> float:
    try:
        num, _, den = str(value).partition("/")
        return float(num) / float(den or 1) if float(den or 1) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _rotation(stream: dict) -> int:
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            return int(round(float(side["rotation"]))) % 360
    try:
        return int((stream.get("tags") or {}).get("rotate", 0)) % 360
    except ValueError:
        return 0


def probe(source: str) -> Probe:
    """Measure a clip (local path or https URL). Raises VideoPrepError."""
    tools = ffmpeg_paths()
    if not tools:
        raise VideoPrepError("Video conversion isn't available: ffmpeg isn't installed on this computer.")
    try:
        out = _run([tools[1], "-v", "error", "-print_format", "json", "-show_format", "-show_streams", source],
                   PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise VideoPrepError("Reading the video took too long. Try uploading it again.")
    try:
        data = json.loads(out.stdout or b"{}")
    except ValueError:
        data = {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    if out.returncode != 0 or not video:
        raise VideoPrepError("That file couldn't be read as a video.")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}

    width, height = int(video.get("width") or 0), int(video.get("height") or 0)
    sar_text = str(video.get("sample_aspect_ratio") or "1:1")
    sar = _ratio(sar_text.replace(":", "/"))
    square = sar_text in ("1:1", "0:1", "N/A") or not sar or abs(sar - 1) < 0.01
    if not square:
        width = int(round(width * sar))
    rotation = _rotation(video)
    if rotation in (90, 270):
        width, height = height, width

    fps = _ratio(video.get("avg_frame_rate") or "") or _ratio(video.get("r_frame_rate") or "")
    duration = float(fmt.get("duration") or video.get("duration") or 0)
    return Probe(
        width=width, height=height, fps=fps, duration=duration,
        size_bytes=int(fmt.get("size") or 0),
        container=str(fmt.get("format_name") or ""),
        vcodec=str(video.get("codec_name") or ""), pix_fmt=str(video.get("pix_fmt") or ""),
        rotation=rotation, square_pixels=square,
        has_audio=bool(audio), acodec=str((audio or {}).get("codec_name") or ""),
        color_transfer=str(video.get("color_transfer") or ""),
        color_primaries=str(video.get("color_primaries") or ""),
        color_space=str(video.get("color_space") or ""),
    )


# ── plan ──────────────────────────────────────────────────────────────────────

@dataclass
class Plan:
    width: int                  # final frame, padding included
    height: int
    scaled_width: int           # picture inside the frame
    scaled_height: int
    fps: float
    seconds: float              # final length
    hold_seconds: float         # last frame held this long to reach the minimum
    encode: bool
    changes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        """Names what the clip was converted to; equal plans share one encode."""
        return f"{self.width}x{self.height}_{self.fps:.3f}fps_{self.seconds:.2f}s"

    def summary(self) -> str:
        return "; ".join(self.changes)


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def _snap_fps(fps: float) -> float:
    nearest = min(_STANDARD_FPS, key=lambda standard: abs(fps - standard))
    return nearest if abs(fps - nearest) / nearest < 0.015 else round(fps, 3)


def _fps_label(fps: float) -> str:
    return f"{fps:.2f}".rstrip("0").rstrip(".")


def make_plan(info: Probe, limits: dict, label: str = "the model") -> Plan:
    """What has to change for `limits`. Raises VideoPrepError if nothing can fix it."""
    min_side, max_side = int(limits.get("min_side") or 0), int(limits.get("max_side") or 0)
    min_fps, max_fps = float(limits.get("min_fps") or 0), float(limits.get("max_fps") or 0)
    min_s, max_s = float(limits.get("min_seconds") or 0), float(limits.get("max_seconds") or 0)
    max_mb = float(limits.get("max_mb") or 0)

    if info.width < 16 or info.height < 16:
        raise VideoPrepError("That video has no usable picture.")
    if info.duration < 0.5:
        raise VideoPrepError("That video is shorter than half a second — upload a longer clip.")

    changes: list[str] = []
    w, h = info.width, info.height

    # Resolution: grow the short side up to the minimum, then shrink if that
    # pushed the long side past the maximum.
    scale = 1.0
    if min_side and min(w, h) < min_side:
        scale = min_side / min(w, h)
    if max_side and max(w, h) * scale > max_side:
        scale = max_side / max(w, h)
    sw, sh = (_even(w * scale), _even(h * scale)) if scale != 1.0 else (w + w % 2, h + h % 2)
    # Rounding can land a pixel outside a bound; nudge back onto it.
    if max_side:
        sw, sh = min(sw, max_side), min(sh, max_side)
    if min_side:
        sw = min_side if min_side - 2 <= sw < min_side else sw
        sh = min_side if min_side - 2 <= sh < min_side else sh
    fw, fh = max(sw, min_side), max(sh, min_side)
    if scale != 1.0:
        verb = "upscaled" if sw * sh > w * h else "downscaled"
        reason = (f"{label} needs at least {min_side}px on each side" if scale > 1
                  else f"{label} accepts at most {max_side}px per side")
        changes.append(f"{verb} from {w}x{h} to {sw}x{sh} ({reason})")
    if (fw, fh) != (sw, sh):
        changes.append(f"padded to {fw}x{fh} because the shape is too wide or tall to fit otherwise")

    fps = _snap_fps(info.fps) if info.fps else (min_fps or 30.0)
    if max_fps and fps > max_fps + 0.01:
        changes.append(f"frame rate lowered from {_fps_label(info.fps)} to {_fps_label(max_fps)} fps")
        fps = max_fps
    elif min_fps and fps < min_fps - 0.01:
        changes.append(f"frame rate raised from {_fps_label(info.fps)} to {_fps_label(min_fps)} fps")
        fps = min_fps

    seconds, hold = info.duration, 0.0
    if max_s and info.duration > max_s + 0.04:
        seconds = max_s
        changes.append(f"trimmed to the first {max_s:g}s ({label} accepts at most {max_s:g}s; the clip is {info.duration:.1f}s)")
    elif min_s and info.duration < min_s:
        seconds = min_s + 0.1
        hold = seconds - info.duration
        changes.append(f"last frame held to make it {min_s:g}s long ({label} needs at least {min_s:g}s)")

    if info.hdr:
        changes.append("HDR converted to standard colour")
    if info.rotation:
        changes.append("phone rotation applied to the picture")

    container_ok = any(c in info.container.split(",") for c in _READY_CONTAINERS)
    format_ok = (container_ok and info.vcodec == "h264" and info.pix_fmt in _READY_PIX_FMTS
                 and info.square_pixels and (not info.has_audio or info.acodec == "aac"))
    size_ok = not max_mb or info.size_bytes <= max_mb * 1024 * 1024
    if not format_ok:
        changes.append(f"converted to MP4 (H.264) from {info.vcodec or 'unknown'}"
                       f"{' in ' + info.container.split(',')[0] if not container_ok else ''}")
    elif not size_ok:
        changes.append(f"compressed under {max_mb:g} MB")

    encode = bool(changes)
    return Plan(width=fw, height=fh, scaled_width=sw, scaled_height=sh, fps=fps,
                seconds=round(seconds, 3), hold_seconds=round(hold, 3), encode=encode,
                changes=changes)


# ── encode ────────────────────────────────────────────────────────────────────

def _video_kbps(plan: Plan, max_mb: float) -> int:
    pixels = plan.width * plan.height
    ceiling = 8000 if pixels <= 1280 * 720 else 16000 if pixels <= 1920 * 1080 else 35000
    if max_mb and plan.seconds:
        # 8% headroom for the container and VBV overshoot.
        budget = int(max_mb * 1024 * 8 * 0.92 / plan.seconds) - _AUDIO_KBPS
        ceiling = min(ceiling, budget)
    return max(500, ceiling)


def _filters(info: Probe, plan: Plan) -> str:
    # Frame rate first and size next, so HDR tone-mapping (float RGB, the slow
    # part) runs on as few and as small frames as possible.
    chain = [f"fps={plan.fps:.6f}"]
    if (plan.scaled_width, plan.scaled_height) != (info.width, info.height) or not info.square_pixels:
        chain.append(f"scale={plan.scaled_width}:{plan.scaled_height}:flags=lanczos")
    if info.hdr:
        tin = info.color_transfer
        pin = info.color_primaries if info.color_primaries.startswith("bt2020") else "bt2020"
        min_ = info.color_space if info.color_space.startswith("bt2020") else "bt2020nc"
        chain += [f"zscale=tin={tin}:pin={pin}:min={min_}:t=linear:npl=100",
                  "format=gbrpf32le", "zscale=p=bt709",
                  "tonemap=tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
    if (plan.width, plan.height) != (plan.scaled_width, plan.scaled_height):
        chain.append(f"pad={plan.width}:{plan.height}:(ow-iw)/2:(oh-ih)/2:color=black")
    chain.append("setsar=1")
    if plan.hold_seconds:
        chain.append(f"tpad=stop_mode=clone:stop_duration={plan.hold_seconds:.3f}")
    chain.append("format=yuv420p")
    return ",".join(chain)


def _encode_args(src: str, dst: str, info: Probe, plan: Plan, limits: dict) -> list[str]:
    kbps = _video_kbps(plan, float(limits.get("max_mb") or 0))
    args = [ffmpeg_paths()[0], "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", src,
            "-map", "0:v:0", "-map", "0:a:0?", "-vf", _filters(info, plan),
            "-fps_mode", "cfr", "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-profile:v", "high", "-maxrate", f"{kbps}k", "-bufsize", f"{kbps * 2}k",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709"]
    if info.has_audio:
        args += ["-c:a", "aac", "-b:a", f"{_AUDIO_KBPS}k", "-ar", "48000", "-ac", "2"]
        if plan.hold_seconds:
            args += ["-af", "apad"]
    args += ["-t", f"{plan.seconds:.3f}", "-map_metadata", "-1", "-movflags", "+faststart", dst]
    return args


def violations(info: Probe, limits: dict) -> list[str]:
    """Everything about a clip that's outside `limits` ([] means acceptable)."""
    problems = []
    min_side, max_side = int(limits.get("min_side") or 0), int(limits.get("max_side") or 0)
    if min_side and min(info.width, info.height) < min_side:
        problems.append(f"{info.width}x{info.height} is under {min_side}px")
    if max_side and max(info.width, info.height) > max_side:
        problems.append(f"{info.width}x{info.height} is over {max_side}px")
    if limits.get("min_fps") and info.fps < float(limits["min_fps"]) - 0.01:
        problems.append(f"{info.fps:.2f} fps is under {limits['min_fps']}")
    if limits.get("max_fps") and info.fps > float(limits["max_fps"]) + 0.01:
        problems.append(f"{info.fps:.2f} fps is over {limits['max_fps']}")
    if limits.get("min_seconds") and info.duration < float(limits["min_seconds"]):
        problems.append(f"{info.duration:.2f}s is under {limits['min_seconds']}s")
    if limits.get("max_seconds") and info.duration > float(limits["max_seconds"]) + 0.05:
        problems.append(f"{info.duration:.2f}s is over {limits['max_seconds']}s")
    if limits.get("max_mb") and info.size_bytes > float(limits["max_mb"]) * 1024 * 1024:
        problems.append(f"{info.size_bytes / 2**20:.0f} MB is over {limits['max_mb']} MB")
    if info.vcodec != "h264" or info.pix_fmt not in _READY_PIX_FMTS or info.rotation:
        problems.append(f"{info.vcodec}/{info.pix_fmt} rotation {info.rotation}")
    return problems


def convert_file(src: str, dst: str, info: Probe, plan: Plan, limits: dict) -> Probe:
    """Encode src -> dst for `plan` and prove the result meets `limits`."""
    args = _encode_args(src, dst, info, plan, limits)
    try:
        out = _run(args, ENCODE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise VideoPrepError("Converting the video took too long. Try a shorter or smaller clip.")
    if out.returncode != 0:
        detail = (out.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
        print(f"[video_prep] ffmpeg failed: {' | '.join(detail)}", file=sys.stderr, flush=True)
        raise VideoPrepError("The video couldn't be converted for this model.")
    result = probe(dst)
    problems = violations(result, limits)
    if problems:
        print(f"[video_prep] converted clip still out of range: {problems}", file=sys.stderr, flush=True)
        raise VideoPrepError("The video couldn't be converted to what this model accepts.")
    return result


# ── prepare a library clip for an engine ──────────────────────────────────────

@dataclass
class Prepared:
    url: str
    width: int
    height: int
    seconds: float
    changes: list[str]


_locks: dict[str, asyncio.Lock] = {}


def limits_for(engine: dict) -> dict:
    return engine.get("input") or {}


async def check(entry: dict, engine: dict) -> Plan:
    """Fast preflight (probe only): the plan for this clip, or VideoPrepError."""
    info = await asyncio.to_thread(probe, entry["url"])
    plan = make_plan(info, limits_for(engine), engine.get("label", "the model"))
    if plan.encode and not ffmpeg_paths():
        raise VideoPrepError("This clip needs converting first, but ffmpeg isn't installed on this computer.")
    return plan


async def _download(url: str, path: str) -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=120), follow_redirects=True) as client:
        async with client.stream("GET", url) as r:
            if r.status_code != 200:
                raise VideoPrepError("The uploaded video couldn't be fetched for converting. Try again.")
            with open(path, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)


def _r2_put(key: str, path: str) -> None:
    from tools.media_upload import _r2
    with open(path, "rb") as f:
        _r2().put_object(Bucket=R2_BUCKET_NAME, Key=key, Body=f, ContentType="video/mp4")


async def prepare(user_id: str, media_id: str, engine: dict, on_convert=None) -> Prepared:
    """The source clip as `engine` accepts it — the original when it already fits.

    `on_convert(plan)` is called once, just before a real encode starts, so the
    job can say what's happening.
    """
    entry = library.get(user_id, media_id)
    if not entry:
        raise VideoPrepError("The source video is no longer in your library. Upload it again.")
    limits = limits_for(engine)
    info = await asyncio.to_thread(probe, entry["url"])
    plan = make_plan(info, limits, engine.get("label", "the model"))
    if not plan.encode:
        return Prepared(entry["url"], info.width, info.height, info.duration, [])

    if on_convert:
        # Before the lock, so a variant waiting on another's encode says why too.
        on_convert(plan)
    lock = _locks.setdefault(f"{user_id}:{media_id}:{plan.key}", asyncio.Lock())
    async with lock:
        cached = ((library.get(user_id, media_id) or {}).get("prepared") or {}).get(plan.key)
        if cached:
            return Prepared(cached["url"], cached["width"], cached["height"], cached["seconds"], plan.changes)

        print(f"[video_prep] {media_id} -> {plan.key}: {plan.summary()}", file=sys.stderr, flush=True)
        with tempfile.TemporaryDirectory(prefix="xelta-prep-") as tmp:
            src = os.path.join(tmp, "source")
            dst = os.path.join(tmp, "prepared.mp4")
            await _download(entry["url"], src)
            result = await asyncio.to_thread(convert_file, src, dst, info, plan, limits)
            # Same folder as the original upload.
            key = f"{R2_UPLOAD_PREFIX}/u/{user_id}/src/{media_id}_{plan.width}x{plan.height}_{math.ceil(plan.fps)}fps.mp4"
            try:
                await asyncio.to_thread(_r2_put, key, dst)
            except Exception as e:
                print(f"[video_prep] R2 put failed key={key}: {e}", file=sys.stderr, flush=True)
                raise VideoPrepError("The converted video couldn't be saved. Try again.")

        prepared = {"url": library.public_url(key), "key": key, "width": result.width,
                    "height": result.height, "seconds": round(result.duration, 3),
                    "size": result.size_bytes, "changes": plan.changes}
        library.set_prepared(user_id, media_id, plan.key, prepared)
        return Prepared(prepared["url"], result.width, result.height, prepared["seconds"], plan.changes)


# ── intake ────────────────────────────────────────────────────────────────────

def sniff_container(head: bytes) -> tuple[str, str, str] | None:
    """(content_type, extension, 'video') for containers beyond MP4/MOV."""
    if head.startswith(b"\x1a\x45\xdf\xa3"):
        return ("video/webm", "webm", "video") if b"webm" in head[:64] else ("video/x-matroska", "mkv", "video")
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return "video/x-msvideo", "avi", "video"
    return None


def probe_bytes(body: bytes, ext: str) -> Probe | None:
    """Probe an in-memory upload; None when ffmpeg is missing or can't read it."""
    if not ffmpeg_paths():
        return None
    fd, path = tempfile.mkstemp(prefix="xelta-probe-", suffix="." + ext)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        return probe(path)
    except VideoPrepError:
        return None
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
