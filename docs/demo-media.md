# Demo footage

Six short, licensed videos are included in ordinary Git under
`data/demo/generated/online/` (about 21 MB combined). Their complete source
records, license links, changes, file sizes, and SHA-256 hashes are in
[`online_manifest.json`](../data/demo/online_manifest.json).

| File | Demo use |
| --- | --- |
| `01_cat_in_can.mp4` | Safety Watch: kitten with head trapped in a can; built-in live sample |
| `02_home_fire.mp4` | Safety Watch: household smoke and fire |
| `03_pet_trapped.mp4` | Safety Watch: dog trapped between fence bars |
| `04_intensive_barking.mp4` | Audible barking and audio-only routing |
| `05_hind_legs.mp4` | Custom visual event: standing on hind legs |
| `06_furniture_chewing.mp4` | Prompt Polish, Smart Routing, event intervals, and alerts |

See [the bundled attribution](../data/demo/generated/online/ATTRIBUTION.txt)
for creator names and the exact license attached to each clip. Source captions,
watermarks, and timing are retained. Videos are demo inputs, not guaranteed
positive/negative model outputs on every run.

Media verification is covered in the [contributor checks](reproduction.md#contributor-checks).

## Long stationary-camera recording

**[4-hour observation camera of a cat alone (Not a pig) — Cat Momi](https://www.youtube.com/watch?v=IBqVxIqLcE0)**

- Source: Cat Momi; the published license was verified as CC BY during development.
- License link: [YouTube Creative Commons Attribution](https://www.youtube.com/t/creative_commons).
- Event-search and SAM results shown in the final demo: **00:00–10:00**,
  with **1,000 sampled frames** for hotspots.

The long recording is not included in Git. Load a local copy through
**Recording analysis → Load a video**
to run a new search or hotspot analysis; the app does not ingest YouTube URLs.
Any local stationary-camera recording can also be used. Exact analysis settings
are in the [reproduction guide](reproduction.md#reproduce-saved-video-search-and-hotspots).

## Finished narrated video

The [three-minute demo](../deliverables/demo-video/pawception-demo-3min.mp4),
[thumbnail](../deliverables/demo-video/pawception-thumbnail.png), and
[credits](../deliverables/demo-video/ATTRIBUTION.txt) are committed.
The video uses actual application captures and saved model results with edited
waiting periods, narration, cursor animation, and diagrams. It is a product
demonstration rather than an unedited inference-speed benchmark.
