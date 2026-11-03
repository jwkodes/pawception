"""Stream SAM pet segmentation and publish evidence snapshots."""

import base64
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
import time

from meta_sam_parser import CompletedOutcome, decode_mask_to_raster, image_segmentation_format, video_segmentation_format
from openai import OpenAI
from PIL import Image, ImageChops, ImageFilter

from pawception.files import checksum, write_json
from pawception.video import ffmpeg
from pawception.muse import BASE_URL, safe_error

COLORS = {"pet": (65, 180, 120), "object": (245, 173, 57)}


def segment_image(client, image, prompt, directory, cancelled=None):
    encoded = base64.b64encode(image.read_bytes()).decode("ascii")
    media = {"type": "input_image", "image_url": "data:image/jpeg;base64," + encoded}
    return segment_media(client, image, media, prompt, directory, cancelled=cancelled)


def segment_video(client, image, file_id, frame_index, prompt, directory, on_frame=None, cancelled=None):
    return segment_media(client, image, {"type": "input_video", "file_id": file_id},
                         prompt, directory, frame_index, on_frame, cancelled)


def mask_image(record, size):
    """Decode a bounded mask into the source frame's coordinate system."""
    if record.mask.width * record.mask.height > size[0] * size[1] * 4:
        raise ValueError("SAM mask dimensions exceed the input frame")
    box = tuple(round(getattr(record.bounds, key)) for key in ("left", "top", "right", "bottom"))
    if not 0 <= box[0] < box[2] <= size[0] or not 0 <= box[1] < box[3] <= size[1]:
        raise ValueError("SAM mask lies outside the frame")
    raster = decode_mask_to_raster(record.mask)
    mask = Image.frombytes("L", (record.mask.width, record.mask.height), raster).point(lambda p: p * 255)
    full = Image.new("L", size)
    full.paste(mask.resize((box[2]-box[0], box[3]-box[1]), Image.Resampling.NEAREST), box[:2])
    return full


def segment_media(client, image, media, prompt, directory, frame_index=None, on_frame=None, cancelled=None):
    """Parse SSE deltas immediately; publish each complete frame before completion."""
    import json
    directory.mkdir(parents=True, exist_ok=True)
    write_json(directory / "request.json", {"model": "sam-3.1", "prompt": prompt,
               "image_sha256": checksum(image), "mask_encoding": "one_bit", "max_retries": 0,
               "media_type": media["type"], "file_id": media.get("file_id"), "snapshot_frame_index": frame_index})
    with Image.open(image) as source:
        image_size = source.size
    parser = (image_segmentation_format() if frame_index is None else video_segmentation_format()).create_parser()
    started = time.monotonic()
    first_frame_seconds = None
    consumed = 0
    segments, tracks = [], []

    def accept(snapshot):
        nonlocal consumed, first_frame_seconds
        if any(item.severity == "error" for item in snapshot.diagnostics):
            raise ValueError("SAM returned invalid segmentation records")
        fresh = snapshot.records[consumed:]
        consumed = len(snapshot.records)
        frames = {}
        for record in fresh:
            if record.kind == "box" and record.frame is not None:
                tracks.append({"frame_index": record.frame.frame_index, "object_id": record.object_id,
                               "box": [record.left, record.top, record.right, record.bottom]})
            if record.kind != "mask":
                continue
            if record.frame is not None:
                frames.setdefault(record.frame.frame_index, []).append(record)
            if frame_index is not None and (record.frame is None or record.frame.frame_index != frame_index):
                continue
            full = mask_image(record, image_size)
            if full.getbbox() is None:
                continue
            filename = f"mask_{len(segments)}.png"
            full.save(directory / filename)
            segments.append({"box": list(full.getbbox()), "mask_file": str(directory / filename),
                             "object_id": record.object_id})
        for index, records in frames.items():
            if first_frame_seconds is None:
                first_frame_seconds = time.monotonic()-started
            if on_frame:
                on_frame(index, records)

    with client.responses.create(model="sam-3.1", store=False, stream=True,
            metadata={"mask_encoding": "one_bit"}, input=[{"role": "user", "content": [
                {"type": "input_text", "text": prompt}, media]}]) as stream:
        response = None
        received_delta = False
        with (directory / "events.jsonl").open("w") as log:
            for event in stream:
                if cancelled is not None and cancelled.is_set():
                    raise CancelledError("Evidence generation stopped")
                log.write(json.dumps(event.model_dump(mode="json")) + "\n")
                if event.type == "response.output_text.delta":
                    received_delta = True
                    for snapshot in parser.push(event.delta):
                        accept(snapshot)
                elif event.type in {"response.completed", "response.failed", "response.incomplete"}:
                    response = event.response
    if response is None or response.status != "completed":
        raise ValueError("SAM response did not complete")
    write_json(directory / "response.json", response.model_dump(mode="json"))
    (directory / "output.txt").write_text(response.output_text)
    # A completed-only fixture or transport can omit deltas. Never parse twice.
    if not received_delta:
        for snapshot in parser.push(response.output_text):
            accept(snapshot)
    finished = parser.finish(CompletedOutcome())
    for snapshot in finished.events:
        accept(snapshot)
    accept(finished.result)
    summary = {"prompt": prompt, "segments": segments, "inference_seconds": time.monotonic()-started,
               "first_frame_seconds": first_frame_seconds,
               "usage": response.model_dump(mode="json").get("usage"),
               "tracking_frames": len({item["frame_index"] for item in tracks})}
    if frame_index is not None:
        write_json(directory / "tracks.json", tracks)
    write_json(directory / "segments.json", summary)
    return summary


def crop_bounds(size, boxes):
    """Keep the pet and nearby context, bounded by the frame."""
    width, height = size
    if not boxes:
        return (0, 0, width, height)
    left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
    right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)
    crop_width = min(width, max((right-left)*2.2, width*0.4))
    crop_height = min(height, max((bottom-top)*2.2, height*0.4, crop_width*0.75))
    crop_width = min(width, max(crop_width, crop_height*4/3))
    x = max(0, min(width-crop_width, (left+right-crop_width)/2))
    y = max(0, min(height-crop_height, (top+bottom-crop_height)/2))
    return (int(x), int(y), min(width, round(x+crop_width)), min(height, round(y+crop_height)))


def render_snapshots(original, concepts, directory):
    image = Image.open(original).convert("RGB")
    overlay = image.copy()
    boxes = {role: [] for role in COLORS}
    for role, color in COLORS.items():
        for segment in concepts.get(role, {}).get("segments", []):
            mask = Image.open(segment["mask_file"]).convert("L")
            tint = Image.new("RGB", image.size, color)
            overlay = Image.composite(tint, overlay, mask.point(lambda p: round(p * 0.32)))
            outline = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(3)), mask)
            overlay.paste(color, mask=outline)
            boxes[role].append(segment["box"])
    crop = crop_bounds(image.size, boxes["object"] or boxes["pet"])
    image.crop(crop).save(directory / "zoom.jpg", quality=92)
    overlay.save(directory / "highlighted.jpg", quality=92)
    return {"crop_box": list(crop), "matches": {role: len(parts) for role, parts in boxes.items()}}


def create_evidence(clip, frame_seconds, prompts, directory, api_key, cancelled=None):
    """Segment one evidence image; publish pet/object results as each completes."""
    directory.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    result = {"status": "processing", "model": "sam-3.1", "media_type": "input_image",
              "frame_seconds": frame_seconds, "prompts": prompts}
    concepts, errors = {}, {}
    revision = 0

    def publish():
        nonlocal revision
        revision += 1
        folder = directory / "snapshots" / f"{revision:06d}"
        folder.mkdir(parents=True, exist_ok=True)
        rendered = render_snapshots(directory / "original.jpg", concepts, folder)
        result.update(rendered, completed_concepts=list(concepts), errors=errors,
                      elapsed_seconds=time.monotonic()-started,
                      **{name: str((folder / f"{name}.jpg").relative_to(directory))
                         for name in ("zoom", "highlighted")})
        write_json(directory / "result.json", result)

    try:
        if cancelled is not None and cancelled.is_set():
            raise CancelledError("Evidence generation stopped")
        result["source_sha256"] = checksum(clip)
        ffmpeg("-y", "-ss", frame_seconds, "-i", clip, "-frames:v", "1", directory / "original.jpg")
        result["original"] = "original.jpg"
        write_json(directory / "result.json", result)
        targets = {role: prompts[role] for role in COLORS if prompts.get(role)}
        if targets:
            with OpenAI(api_key=api_key, base_url=BASE_URL, timeout=45, max_retries=0) as client:
                # Two bounded image requests share the extracted JPEG, never a video.
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="sam-image") as executor:
                    futures = {executor.submit(segment_image, client, directory / "original.jpg", prompt,
                                              directory / role, cancelled): role for role, prompt in targets.items()}
                    for future in as_completed(futures):
                        role = futures[future]
                        if cancelled is not None and cancelled.is_set():
                            raise CancelledError("Evidence generation stopped")
                        try:
                            concepts[role] = future.result()
                        except Exception as error:
                            errors[role] = safe_error(error)
                        publish()
        if not result.get("zoom"):
            publish()
        result["status"] = "ready" if any(result["matches"].values()) else "unavailable" if errors else "no_matches"
    except Exception as error:
        result.update(status="unavailable", error=safe_error(error))
    result["elapsed_seconds"] = time.monotonic()-started
    write_json(directory / "result.json", result)
    return result
