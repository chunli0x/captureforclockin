"""Private local bucket and resource/network/filesystem isolated decoding."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

from .media import MediaError, dhash, strip_metadata, DURATION_MS, WIDTH, HEIGHT, FPS


def bucket(settings, area):
    if area not in {"quarantine", "review", "approved", "work"}:
        raise MediaError()
    root = settings.media_root.resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    path = root / area
    path.mkdir(exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def asset_path(settings, area, identity):
    if not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise MediaError()
    return bucket(settings, area) / (identity + ".mp4")


def write_private(path, data):
    # Atomic publish; never expose a partial file to a reviewer or decoder.
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sandbox(directory, command):
    # No host root, secrets, network, or writable filesystem except this job's
    # directory. prlimit bounds CPU, address space, outputs and open descriptors.
    args = ["/usr/bin/prlimit", "--cpu=45", "--as=2147483648", "--fsize=67108864", "--nofile=64", "--",
            "/usr/bin/bwrap", "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
            "--ro-bind", "/usr", "/usr", "--ro-bind", "/lib", "/lib", "--ro-bind", "/lib64", "/lib64",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--bind", str(directory), "/work",
            "--chdir", "/work", "--clearenv", "--setenv", "PATH", "/usr/bin", "--setenv", "HOME", "/tmp",
            "--", *command]
    try:
        result = subprocess.run(args, capture_output=True, timeout=60, check=True)
        return result.stdout
    except Exception:
        # ffmpeg stderr may repeat source metadata. Never propagate or log it.
        raise MediaError() from None


def probe(directory, name):
    raw = sandbox(directory, ["/usr/bin/ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
                              "-show_streams", "-show_format", "-of", "json", name])
    return json.loads(raw)


def transcode(source, work_root):
    with tempfile.TemporaryDirectory(dir=work_root) as temp:
        directory = Path(temp)
        # Input has no original path/name/extension inside the sandbox.
        (directory / "input.mp4").write_bytes(source.read_bytes())
        info = probe(directory, "input.mp4")
        streams = info.get("streams", [])
        videos = [s for s in streams if s.get("codec_type") == "video"]
        try:
            duration = float(info["format"]["duration"])
            valid = (len(videos) == 1 and 0.5 <= duration <= 30 and
                     0 < int(videos[0]["width"]) <= 3840 and 0 < int(videos[0]["height"]) <= 2160 and
                     info["format"]["format_name"] == "mov,mp4,m4a,3gp,3g2,mj2")
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise MediaError()
        sandbox(directory, ["/usr/bin/ffmpeg", "-v", "error", "-nostdin", "-xerror", "-threads", "1",
            "-protocol_whitelist", "file,pipe", "-i", "input.mp4", "-map", "0:v:0", "-an", "-sn", "-dn",
            "-map_metadata", "-1", "-map_chapters", "-1", "-filter_threads", "1",
            "-vf", "scale=640:360:force_original_aspect_ratio=decrease,pad=640:360:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=24,tpad=stop_mode=clone:stop_duration=4",
            "-t", "4", "-c:v", "libx264", "-threads", "1", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-bsf:v", "filter_units=remove_types=6", "-movflags", "+faststart",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "output.mp4"])
        data = strip_metadata((directory / "output.mp4").read_bytes())
        (directory / "clean.mp4").write_bytes(data)
        result = probe(directory, "clean.mp4")
        if (len(result["streams"]) != 1 or result["streams"][0]["codec_name"] != "h264"
                or abs(float(result["format"]["duration"]) - 4) > 0.05):
            raise MediaError()
        # Decode the ENTIRE derivative, not a byte sample. Each rendered frame
        # contributes a perceptual hash; encoding/container changes don't evade it.
        pixels = sandbox(directory, ["/usr/bin/ffmpeg", "-v", "error", "-nostdin", "-xerror", "-threads", "1",
            "-protocol_whitelist", "file,pipe", "-i", "clean.mp4", "-an", "-filter_threads", "1",
            "-vf", "scale=9:8,format=gray", "-f", "rawvideo", "-threads", "1", "pipe:1"])
        if len(pixels) != 72 * FPS * DURATION_MS // 1000:
            raise MediaError()
        hashes = [dhash(pixels[i:i + 72]) for i in range(0, len(pixels), 72)]
        return data, dict(sha256=hashlib.sha256(data).hexdigest(), dhashes=json.dumps(hashes),
                          duration_ms=DURATION_MS, width=WIDTH, height=HEIGHT, fps=FPS, byte_size=len(data))
