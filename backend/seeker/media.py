"""Robot vision video policy. No source names, remote URLs or provider payloads escape."""
import base64
import hashlib
import hmac
import json
import math
import struct

import httpx

from .db import now

SIGHTENGINE_VIDEO_ENDPOINT = "https://api.sightengine.com/1.0/video/check-sync.json"
DURATION_MS, WIDTH, HEIGHT, FPS = 4000, 640, 360, 24


class MediaError(Exception):
    def __init__(self, code="processing_failed"):
        self.code = code
        super().__init__(code)


def face_clear(frame):
    # Missing, null, malformed, or partial detection is never a pass.
    return (isinstance(frame, dict) and isinstance(frame.get("faces"), list) and frame["faces"] == []
            and isinstance(frame.get("artificial_faces"), list) and frame["artificial_faces"] == [])


def inspection_clear(payload):
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return False
    data = payload.get("data")
    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list) or len(frames) < 8:
        return False
    positions = []
    for frame in frames:
        if not face_clear(frame):
            return False
        text = frame.get("text")
        if not isinstance(text, dict) or text.get("boxes") != []:
            return False
        for field in ("has_artificial", "has_natural"):
            value = text.get(field)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value < 0.1:
                return False
        info = frame.get("info")
        position = info.get("position") if isinstance(info, dict) else None
        if type(position) not in (int, float) or not math.isfinite(position):
            return False
        positions.append(position)
    positions.sort()
    # Sightengine frame positions are milliseconds; interval requests are seconds.
    return (positions[0] == 0 and 3500 <= positions[-1] <= DURATION_MS
            and all(0 < b - a <= 510 for a, b in zip(positions, positions[1:])))


def inspect_video(path, settings):
    if not settings.sightengine_user or not settings.sightengine_secret:
        raise MediaError("inspection_failed")
    try:
        # Endpoint allowlist has ONE member. Upload bytes; never let a provider
        # fetch a supplied URL. Disable redirects and environment proxy routing.
        with httpx.Client(timeout=90, follow_redirects=False, trust_env=False) as client, path.open("rb") as clip:
            response = client.post(SIGHTENGINE_VIDEO_ENDPOINT, data={
                "api_user": settings.sightengine_user, "api_secret": settings.sightengine_secret,
                "models": "face-analysis,text", "interval": "0.5",
            }, files={"media": ("clip.mp4", clip, "video/mp4")})
        response.raise_for_status()
        if not inspection_clear(response.json()):
            raise MediaError("inspection_failed")
    except Exception:
        raise MediaError("inspection_failed") from None


def dhash(gray_9x8):
    """64-bit perceptual difference hash of DECODED, scaled grayscale pixels."""
    if len(gray_9x8) != 72:
        raise MediaError()
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | (gray_9x8[row * 9 + col] > gray_9x8[row * 9 + col + 1])
    return f"{value:016x}"


def perceptual_duplicate(left, right):
    if not left or len(left) != len(right):
        return False
    distances = [(int(a, 16) ^ int(b, 16)).bit_count() for a, b in zip(left, right)]
    return max(distances) <= 8 and sum(distances) / len(distances) <= 4


# Unknown boxes (including unknown containers) are erased in their entirety.
# Recursion handles every retained container, including sample descriptions.
# Keeping byte lengths preserves stco/co64 offsets into mdat.
CONTAINERS = {b"moov": 0, b"trak": 0, b"mdia": 0, b"minf": 0, b"stbl": 0,
              b"edts": 0, b"dinf": 0, b"mvex": 0, b"moof": 0, b"traf": 0,
              b"mfra": 0, b"stsd": 8, b"dref": 8, b"avc1": 78}
LEAVES = {b"ftyp", b"mdat", b"mvhd", b"tkhd", b"mdhd", b"hdlr", b"vmhd",
          b"stts", b"ctts", b"stsc", b"stsz", b"stco", b"co64", b"stss",
          b"elst", b"avcC", b"pasp", b"btrt", b"url ", b"trex", b"mfhd",
          b"tfhd", b"tfdt", b"trun", b"mehd", b"tfra", b"mfro"}


def strip_metadata(blob):
    out = bytearray(blob)

    def walk(start, end, depth=0):
        if depth > 24:
            raise MediaError()
        pos = start
        while pos < end:
            if end - pos < 8:
                raise MediaError()
            size, kind = struct.unpack_from(">I4s", out, pos)
            header = 8
            if size == 1:
                if end - pos < 16:
                    raise MediaError()
                size = struct.unpack_from(">Q", out, pos + 8)[0]
                header = 16
            elif size == 0:
                size = end - pos
            if size < header or pos + size > end:
                raise MediaError()
            body, stop = pos + header, pos + size
            if kind in CONTAINERS:
                prefix = CONTAINERS[kind]
                if stop - body < prefix:
                    raise MediaError()
                if kind == b"avc1":
                    # VisualSampleEntry compressorname is not a child atom.
                    out[body + 42:body + 74] = bytes(32)
                walk(body + prefix, stop, depth + 1)
            elif kind not in LEAVES:
                out[pos + 4:pos + 8] = b"free"
                out[body:stop] = bytes(stop - body)
            elif kind == b"hdlr":
                if stop - body < 24:
                    raise MediaError()
                out[body + 24:stop] = bytes(stop - body - 24)
            elif kind == b"url ":
                # Only a self-contained media data reference is permitted.
                if stop - body != 4 or out[body:stop] != b"\x00\x00\x00\x01":
                    raise MediaError()
            elif kind in (b"mvhd", b"tkhd", b"mdhd"):
                # Creation and modification times can encode source identity.
                if stop - body < 12 or out[body] not in (0, 1):
                    raise MediaError()
                count = 16 if out[body] == 1 else 8
                if stop - body < 4 + count:
                    raise MediaError()
                out[body + 4:body + 4 + count] = bytes(count)
            pos = stop
    walk(0, len(out))
    return bytes(out)


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def sign_delivery(payload, secret):
    if not secret:
        raise MediaError()
    data = _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    mac = _b64(hmac.digest(secret.encode(), data.encode(), "sha256"))
    return data + "." + mac


def verify_delivery(token, secret, wallet):
    try:
        if not secret or len(token) > 2048:
            raise ValueError
        data, mac = token.split(".")
        expected = _b64(hmac.digest(secret.encode(), data.encode(), "sha256"))
        if not hmac.compare_digest(mac, expected):
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        if payload["wallet"] != wallet or type(payload["expires"]) is not int or payload["expires"] <= now():
            raise ValueError
        return payload
    except Exception:
        raise MediaError() from None
