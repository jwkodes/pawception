"""Command entry point for :mod:`pawception.video`."""

# Support both `python -m scripts.downres_videos` and direct script execution.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawception.video import main


if __name__ == "__main__":
    raise SystemExit(main())
