"""Command entry point for :mod:`pawception.webcam_smoke`."""

# Support both `python -m scripts.webcam_browser_smoke` and direct script execution.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawception.webcam_smoke import main


if __name__ == "__main__":
    raise SystemExit(main())
