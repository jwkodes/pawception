"""Prepare timestamp-aligned audio and visual inputs from a captured window."""

from subprocess import SubprocessError

from pawception.files import checksum
from pawception.video import ffmpeg


def has_audio(path):
    try:
        result = ffmpeg("-i", path, "-map", "0:a:0", "-t", "0.02", "-vn", "-f", "framehash", "-")
        return any(line and not line.startswith("#") for line in result.stdout.splitlines())
    except (OSError, SubprocessError):
        return False


def media_window(directory, window, kind, audio_present):
    """Video stays at its source frame rate; audio is mono 16 kHz PCM WAV."""
    source = directory / window["file"]
    folder = source.parent
    target = source
    if kind == "audio":
        target = folder / "audio.wav"
        if not target.exists():
            ffmpeg("-y", "-i", source, "-map", "0:a:0", "-vn", "-t", window["input_duration_seconds"],
                   "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", target)
    elif kind == "visual" and audio_present:
        target = folder / "visual.mp4"
        if not target.exists():
            ffmpeg("-y", "-i", source, "-map", "0:v:0", "-an", "-c:v", "copy",
                   "-movflags", "+faststart", target)
    return {**window, "file": str(target.relative_to(directory)), "sha256": checksum(target)}
