# Third-party dependencies and models

The base app and optional local geometry use separate environments.
`pyproject.toml`, `uv.lock`, and `.python-version` pin the application environment.
No model weights are committed. Download records in
[`data/model_sources.json`](../data/model_sources.json) pin URLs, sizes, and SHA-256
hashes; `scripts.prepare_models` verifies an existing file before reusing it.

## What each feature requires

| Feature | Dependencies | Where inference runs |
| --- | --- | --- |
| UI, mock monitoring, timeline, local video processing | Base `uv sync --locked` | Local Python server and browser |
| Prompt Polish, Smart Routing, visual events, Safety Watch, saved-video search | Meta API key; Muse Spark 1.3 Contributor by default | Meta Model API |
| Audio / combined event checks | Meta API key; Muse Spark 1.2 Contributor by default | Meta Model API |
| SAM hotspots | Meta API key; `sam-3.1`; `meta-sam-parser` | Meta Model API; mask decoding and heatmap aggregation are local |
| Change-detection hotspots | Base environment; Pillow and FFmpeg | Local; no model or API key |
| 3D hotspots | Optional `.venv-moge` and downloaded MoGe checkpoint | Local; saved masks are reused |

Muse and SAM are hosted services; Pawception does not download their weights.
The SDK endpoint is `https://api.meta.ai/v1`. The Contributor variants are
explicitly labeled in the UI; see [Meta's documentation](https://dev.meta.ai/docs/)
for API access, terms, and current model availability.

## Base Python environment

| Dependency | Pinned version | Purpose | License / upstream |
| --- | --- | --- | --- |
| `openai` | 3.39.0 | Compatible API transport to Meta; no OpenAI inference calls | [Apache-2.0](https://github.com/openai/openai-python) |
| `meta-sam-parser` | 0.1.0 | Decode streamed SAM results | [SAM License](https://github.com/meta-models/meta-sam/blob/main/LICENSE) |
| `imageio-ffmpeg` | 0.6.0 | Pinned FFmpeg runtime for preparation, windows, audio, and evidence | [BSD-2-Clause wrapper](https://github.com/imageio/imageio-ffmpeg); the FFmpeg executable has its [own build-dependent license](https://ffmpeg.org/legal.html) |
| `Pillow` | 13.0.0 | Image processing, masks, thumbnails, and heatmaps | [MIT-CMU](https://github.com/python-pillow/Pillow) |
| `python-dotenv` | 1.2.4 | Read the local `.env` file | [BSD-3-Clause](https://github.com/theskumar/python-dotenv) |
| `httpx2` (development group) | 2.16.0 | Transport used by tests | [BSD-3-Clause](https://github.com/pydantic/httpx2) |

Transitive package versions and distribution hashes are recorded in `uv.lock`.
The UI uses plain browser JavaScript and WebGL; it has no npm build or CDN library
dependency. Node is needed only to run the JavaScript tests. SQLite comes from
the Python standard library and requires no database server.

## MoGe: optional local depth for 3D hotspots

Pawception uses **MoGe-2 Small**, with the fast `resolution_level=0` setting.
MoGe predicts depth and camera geometry; it is a Microsoft model, separate from
the Meta models used for understanding and segmentation.

- Code: [Microsoft MoGe](https://github.com/microsoft/MoGe), pinned commit
  `925b8ed835a7a9cdb7578ba15c658a0afc969030`.
- Checkpoint: [`Ruicheng/moge-2-vits-normal`](https://huggingface.co/Ruicheng/moge-2-vits-normal),
  pinned revision `26b477f41595707c5db6770294c0d1721e8ed4ed`.
- Weight file: `data/models/moge-2-vits-normal/model.pt`, about 140.6 MB.
  Exact download URLs, sizes, and SHA-256 hashes are recorded in
  [`data/model_sources.json`](../data/model_sources.json).
- License: upstream code is MIT with additional third-party notices in its
  [pinned license file](https://github.com/microsoft/MoGe/blob/925b8ed835a7a9cdb7578ba15c658a0afc969030/LICENSE).
  The checkpoint's [model page](https://huggingface.co/Ruicheng/moge-2-vits-normal)
  identifies its license as MIT.
Install it using the [3D setup instructions](../README.md#optional-moge-setup-for-3d).
The `moge` dependency group and its full transitive dependency graph are pinned
in `pyproject.toml` and `uv.lock`.

The web app specifically looks for `.venv-moge/bin/python` and the weight path
above. The separate environment keeps the larger geometry stack out of the base
app. The worker selects MPS on Apple Silicon, otherwise CUDA when available,
otherwise CPU. It uses native PyTorch attention and does not require Triton;
CPU inference can be slow. The current workflow has been exercised on Apple
Silicon, not benchmarked across every device.

Each environment has its own Python launcher and `pyvenv.cfg`. The server runs
in `.venv`; [the 3D worker](../pawception/offline_spatial.py) explicitly launches
`.venv-moge/bin/python`, which uses that environment's installed packages.
No shell activation is required when using this path.

## Footage and final video

See [demo media](demo-media.md) for source links and the attribution files that
accompany the bundled footage and final video.
