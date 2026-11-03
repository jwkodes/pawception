"""Command entry point for :mod:`pawception.evaluation_runner`."""

# Support both `python -m scripts.evaluate_submission` and direct script execution.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawception.evaluation_runner import main


if __name__ == "__main__":
    raise SystemExit(main())
