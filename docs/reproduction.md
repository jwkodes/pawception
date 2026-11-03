# Reproducing Pawception

The repository includes the application, locked dependencies, current prompts
and schemas, licensed short demo clips, reviewed labels, and saved evaluation
observations, together with the final demo MP4.

## Install and start

Follow the [README](../README.md#run-locally). Start with Mock mode to check
the interface without API access. Mock detections are scripted rather than
inferences about the selected footage.

For real understanding, add your own Meta key to `.env` and select Muse mode.
The following are the settings used for the demonstrated monitoring flows:

| Setting | Value |
| --- | --- |
| Prompt Polish and Smart Routing | Muse Spark 1.3 Contributor, minimal reasoning |
| Visual understanding and Safety Watch | Muse Spark 1.3 Contributor, medium reasoning |
| Audio understanding | Muse Spark 1.2 Contributor, medium reasoning |
| Window / overlap | 60 seconds / 0 seconds |
| Event gap tolerance | 1 second |
| Custom alert threshold | 5 seconds of detected activity |
| Live segmentation | Disabled; the live path does not run SAM |

Short video replays submit the final partial window when playback finishes.
The five-second alert setting measures detected source-video activity; it is
not a five-second model-response deadline. The video edit shortens waiting periods.

## Try the live scenarios

The clips are in `data/demo/generated/online/`; creator credits and hashes are
in the accompanying manifest and attribution file.

- **Furniture chewing:** choose `06_furniture_chewing.mp4`. Use the expanded
  description in [`monitor_task.json`](../prompts/examples/monitor_task.json).
  Prompt Polish offers editable rewrites; Smart Routing resolves this to visual.
- **Barking:** choose `04_intensive_barking.mp4` and describe repeated audible
  barking. Select Auto or Audio. A separate audio model receives audio only.
- **Safety Watch:** use `01_cat_in_can.mp4` or `02_home_fire.mp4` with Safety Watch
  on. These do not need a custom event. The default hazard guidance is exported
  in [`safety_watch.default.txt`](../prompts/examples/safety_watch.default.txt).

Model output can vary across requests. Saved detections document the observed
development runs rather than guaranteeing that a fresh request has identical
boundaries. The [prompt catalog](../prompts/README.md) shows how complete requests
are assembled and where each runtime instruction originates.

## Reproduce saved-video search and hotspots

The [long recording source](demo-media.md#long-stationary-camera-recording) is
linked rather than committed. Load a local copy through Recording analysis.
The demo uses source time **00:00–10:00**:

1. Search for **Cat on the bed** with Auto inputs and Spark 1.3 Contributor,
   medium reasoning, using 60-second non-overlapping windows.
2. For SAM hotspots, describe the pet as **orange tabby cat** and cap sampling
   at **1,000 frames** over that ten-minute range. Preview masks as they arrive.
3. Open 3D hotspots after completing SAM. The optional MoGe setup is documented
   in [dependencies](dependencies.md#moge-optional-local-depth-for-3d-hotspots).
   The demonstrated saved map uses MoGe fast with a fixed horizontal FOV of
   approximately **71.9°** and a voxel size of **0.08 predicted metres**.

Local change detection is an alternative to SAM for a stationary-camera scene
with one moving pet. Both methods feed the same 3D center calculation.
2D hotspots use the whole mask; 3D hotspots use one center per located sample.
Depth, geometry, and occupancy time are estimates.

## Recompute the reported evaluation

The committed snapshot contains the human annotations and final saved
observations. Recompute scores without downloading footage, calling models,
or overwriting the report:

```sh
uv run --locked python -m scripts.verify_evaluation
```

Expected result: **54 scored clip/event decisions across eight clips**, with
mean positive temporal IoU **0.832263565574**. The verifier also checks that the
recorded evaluation revisions are preserved.

To collect new model responses, restore the additional small Pexels evaluation
inputs and review the call plan first:

```sh
uv run --locked python -m scripts.prepare_test_videos
uv run --locked python -m scripts.evaluate_submission --dry-run
# Makes real API calls and writes a new run:
uv run --locked python -m scripts.evaluate_submission
```

The historical final report combines the initial suite with a visual rerun of
the furniture clip using the expanded description, and excludes an ambiguous
eating-at-bowl clip. These revisions are documented in the report and saved run;
the evaluator starts a new suite and does not silently replay those revisions.
Fresh outputs are not expected to exactly reproduce the frozen scores.
This is a small development evaluation, not a held-out safety benchmark.

## Contributor checks

These checks are optional development workflows and are not needed to run Pawception:

```sh
uv run --locked python -m unittest discover -s tests
node --test tests/*.cjs
uv run --locked python -m scripts.verify_demo_assets --decode
uv run --locked python -m scripts.export_prompts --check
```

Node is used only for the JavaScript tests. Model-call tests use mocks and do
not spend API credits. Optional geometry tests skip when their dependencies
are absent.

The files in `prompts/examples/` are readable snapshots exported from the Python
runtime builders. `scripts.export_prompts --check` rebuilds the examples in
memory and compares them with the committed snapshots. A match prints
`Runtime prompt snapshots verified.` A mismatch reports an outdated snapshot
and exits with an error. The check does not overwrite files or call model APIs.
After editing a runtime prompt builder, run the command without `--check` to
refresh the snapshots before committing.
