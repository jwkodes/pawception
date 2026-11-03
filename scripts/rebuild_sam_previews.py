"""Command entry point for :mod:`pawception.sam_preview_rebuild`."""

# Support both `python -m scripts.rebuild_sam_previews` and direct script execution.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawception.sam_preview_rebuild import main


if __name__ == "__main__":
    raise SystemExit(main())
