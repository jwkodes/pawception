"""Command entry point for :mod:`pawception.evaluation_checks`."""

# Support both `python -m scripts.verify_evaluation` and direct script execution.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawception.evaluation_checks import main


if __name__ == "__main__":
    raise SystemExit(main())
