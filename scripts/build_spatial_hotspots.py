"""Command entry point for :mod:`pawception.spatial_worker`."""

# Support both `python -m scripts.build_spatial_hotspots` and direct script execution.
if __package__ in (None, ""):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pawception.spatial_worker import main


if __name__ == "__main__":
    raise SystemExit(main())
