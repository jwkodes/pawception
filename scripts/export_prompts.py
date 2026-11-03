"""Command entry point for :mod:`pawception.prompt_exports`."""

# Support both `python -m scripts.export_prompts` and direct script execution.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawception.prompt_exports import main


if __name__ == "__main__":
    raise SystemExit(main())
