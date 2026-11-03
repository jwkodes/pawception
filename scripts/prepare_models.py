"""Command entry point for :mod:`pawception.model_downloads`."""

# Support both `python -m scripts.prepare_models` and direct script execution.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawception.model_downloads import main


if __name__ == "__main__":
    raise SystemExit(main())
