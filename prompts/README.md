# Prompts and model contracts

## Current application

The current application builds prompts from the owner's event definitions,
the window duration, the selected inputs, and the current Safety Watch settings.
It makes one structured request per model group, covering all events assigned
to that group. Temporal merging and alert thresholds run in Python after inference.

| Feature | Runtime source | Reviewable example |
| --- | --- | --- |
| Prompt Polish: three complete description rewrites | [event_suggestions.py](../pawception/event_suggestions.py) | [System prompt](examples/event_enrichment.system.txt), [schema](examples/event_enrichment.schema.json) |
| Smart Routing: visual, audio, or both | [event_inputs.py](../pawception/event_inputs.py) | [System prompt](examples/event_routing.system.txt), [schema](examples/event_routing.schema.json) |
| Visual event detection and Safety Watch | [pipeline.py](../pawception/pipeline.py), [monitor.py](../pawception/monitor.py), [safety.py](../pawception/safety.py) | [Rendered prompt](examples/visual_monitor.prompt.txt), [schema](examples/visual_monitor.schema.json), [default hazard guidance](examples/safety_watch.default.txt) |
| Audio-only event detection | [pipeline.py](../pawception/pipeline.py), [audio_inference.py](../pawception/audio_inference.py) | [Rendered prompt](examples/audio_monitor.prompt.txt), [schema](examples/audio_monitor.schema.json) |
| Evidence frame and bounding boxes | [evidence_boxes.py](../pawception/evidence_boxes.py), [evidence_prompts.py](../pawception/evidence_prompts.py) | Included in the visual example above |
| SAM pet segmentation for hotspots | [offline_sam.py](../pawception/offline_sam.py) | The owner's pet description, such as `orange tabby cat`, is sent directly to `sam-3.1` with the sampled footage. No LLM system prompt is added. |

## What is in `examples/`?

These are readable snapshots of the current application prompts and output
contracts, useful for inspecting and reproducing requests:

| Files | Contents |
| --- | --- |
| `event_enrichment.system.txt` and `.schema.json` | Prompt Polish instructions and structured output format for three complete rewrites. |
| `event_routing.system.txt` and `.schema.json` | Smart Routing instructions and the visual/audio/both response format. |
| `visual_monitor.prompt.txt` and `.schema.json` | A fully rendered 60-second furniture-monitoring example, including Safety Watch and evidence boxes. |
| `audio_monitor.prompt.txt` and `.schema.json` | A fully rendered 60-second audio-only barking example and its response format. |
| `safety_watch.default.txt` | The current default guidance for visible risks to pets, people, and the home. |
| `monitor_task.json` | Event definitions and settings used to generate the visual example, including the expanded furniture description from the demo. |

The app builds actual requests from the events and settings entered in the UI.
It substitutes the real window duration and known hazard identities; it does
not load these snapshots to configure monitoring. Editing an example file does
not change the app's runtime instructions. No model responses or detections are
stored in this directory. Exact submitted prompts and schemas are also saved
with each local inference run.

Snapshot verification and refresh instructions are in the
[contributor checks](../docs/reproduction.md#contributor-checks).

## Legacy standalone templates

These four files are retained for the
[standalone experiment runner](../pawception/muse.py), launched through
`python -m scripts.muse_video_experiment`. They
are not used by the current live-monitoring or saved-recording event pipeline.

| Template | Purpose | Runner option |
| --- | --- | --- |
| [`video_description.txt`](video_description.txt) | Free-form chronological video description, animals/objects, approximate timestamps, and uncertainties. | `--mode describe` |
| [`video_hazards.txt`](video_hazards.txt) | Free-form review of potential animal-related hazards and supporting evidence. This is an earlier experiment, not the current whole-scene Safety Watch guidance. | `--mode hazards` |
| [`video_monitor.txt`](video_monitor.txt) | Detailed observed/not-observed/unknown assessments with visibility, event intervals, uncertainty, and coverage gaps. | `--mode monitor --monitor-format temporal` |
| [`video_monitor_simple.txt`](video_monitor_simple.txt) | Shorter observed/not-observed/unknown assessments with intervals, evidence, and uncertainty; no visibility or coverage-gap fields. | `--mode monitor --monitor-format simple` |

Both monitor modes require a `--monitor-task`. The current application instead
uses the yes/no detection contract shown in `examples/visual_monitor.schema.json`
and `examples/audio_monitor.schema.json`.
