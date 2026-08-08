"""Conservative source-quality policy for audit-proposed candidates."""

import json
import subprocess

LOSSLESS_CODECS = {"flac", "alac", "wav", "aiff", "ape"}


class ProbeError(RuntimeError):
    pass


class FFprobe:
    def __init__(self, executable="ffprobe", timeout=10, run=subprocess.run):
        self.executable, self.timeout, self.run = executable, timeout, run

    def probe(self, path):
        args = [self.executable, "-v", "error", "-show_entries",
                "format=format_name,duration:stream=codec_name,bit_rate,sample_rate,bits_per_raw_sample,bits_per_sample,channels",
                "-select_streams", "a:0", "-of", "json", str(path)]
        try:
            result = self.run(args, capture_output=True, text=True, timeout=self.timeout,
                              check=False, shell=False)
            if result.returncode != 0:
                raise ValueError
            data = json.loads(result.stdout)
            stream = (data.get("streams") or [])[0]
            fmt = data.get("format") or {}
            integer = lambda value: int(value) if value not in (None, "", "N/A", "0") else None
            number = lambda value: float(value) if value not in (None, "", "N/A") else None
            codec = stream.get("codec_name")
            return {"codec": codec, "container": fmt.get("format_name"),
                    "bitrate": integer(stream.get("bit_rate")),
                    "sample_rate": integer(stream.get("sample_rate")),
                    "bit_depth": integer(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample")),
                    "channels": integer(stream.get("channels")), "duration": number(fmt.get("duration")),
                    "lossless": codec.lower() in LOSSLESS_CODECS if codec else None}
        except (subprocess.TimeoutExpired, OSError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError) as exc:
            raise ProbeError("media probe unavailable") from None


def media_quality(media_info, probed, size):
    media_info = media_info or {}
    codec = media_info.get("audioCodec") or media_info.get("audioFormat")
    facts = {
        "codec": codec.lower() if isinstance(codec, str) else None,
        "container": media_info.get("containerFormat"),
        "bitrate": media_info.get("audioBitrate"),
        "sample_rate": media_info.get("audioSampleRate"),
        "bit_depth": media_info.get("audioBits"),
        "channels": media_info.get("audioChannels"),
        "duration": media_info.get("duration"),
    }
    missing = [key for key, value in facts.items() if value is None]
    for key in missing:
        facts[key] = (probed or {}).get(key)
    facts["lossless"] = ((probed or {}).get("lossless") if facts["codec"] is None
                         else facts["codec"] in LOSSLESS_CODECS)
    facts["size_band"] = "small" if size < 10_000_000 else "medium" if size < 50_000_000 else "large"
    facts["source"] = "lidarr+probe" if probed and media_info else "lidarr" if media_info else "probe"
    facts["confidence"] = "measured" if probed else "reported"
    return facts


def quality_decision(current, candidate, *, edition_match=None, identity_verified=True):
    """Return ``review_only`` or ``rejected``; never auto-upgrade YouTube.

    Identity and source quality are intentionally separate gates. Container,
    extension and claimed bitrate do not establish a material improvement.
    ``edition_match`` is tri-state: only an explicit known mismatch rejects;
    FileVerifier's accepted recording match has no separate edition evidence.
    """
    if not identity_verified or edition_match is False:
        return "rejected"
    if current.get("verified") and str(current.get("codec", "")).lower() in LOSSLESS_CODECS:
        return "rejected"
    # No YouTube-derived source fact can prove quality solely from a label or
    # bitrate. A human may inspect a verified staged candidate instead.
    return "review_only"
