"""Command entry point for :mod:`pawception.temporal_demo`."""

# Support both `python -m scripts.temporal_demo` and direct script execution.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawception.temporal_demo import main


if __name__ == "__main__":
    raise SystemExit(main())
