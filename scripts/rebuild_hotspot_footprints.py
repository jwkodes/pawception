"""Command entry point for :mod:`pawception.hotspot_rebuild`."""

# Support both `python -m scripts.rebuild_hotspot_footprints` and direct script execution.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawception.hotspot_rebuild import main


if __name__ == "__main__":
    raise SystemExit(main())
