"""Publish timestamped, transparent pet masks while a SAM stream is running."""

from PIL import Image, ImageChops, ImageFilter

from pawception.sam_evidence import COLORS, mask_image
from pawception.files import write_json


class TrackWriter:
    def __init__(self, directory, size, timeline):
        self.directory, self.size, self.timeline = directory, size, timeline
        (directory / "frames").mkdir(parents=True, exist_ok=True)
        self.frames = {}
        self.object_id = None
        self.ambiguous = False
        self.status = "processing"
        self.revision = 0

    def add(self, index, records):
        self.revision += 1
        if not 0 <= index < len(self.timeline):
            raise ValueError("SAM frame lies outside the uploaded video")
        if self.object_id is None and not self.ambiguous:
            ids = {record.object_id for record in records}
            if len(ids) != 1:
                self.ambiguous = True  # Never silently highlight several lookalike pets.
            else:
                self.object_id = next(iter(ids))
        matches = [record for record in records if record.object_id == self.object_id]
        if not self.ambiguous and matches:
            mask = mask_image(matches[-1], self.size)
            overlay = Image.new("RGBA", self.size, (*COLORS["pet"], 0))
            alpha = mask.point(lambda p: round(p*0.32))
            outline = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(3)), mask)
            overlay.putalpha(ImageChops.lighter(alpha, outline))
            filename = f"{index:06d}.png"
            target = self.directory / "frames" / filename
            temporary = target.with_suffix(".tmp")
            overlay.save(temporary, format="PNG")
            temporary.replace(target)
            pts, length = self.timeline[index]
            self.frames[index] = {"index": index, "start": float(pts), "end": float(pts+length),
                                  "file": filename, "revision": self.revision}
        self.publish()

    def finish(self, status="ready"):
        if self.ambiguous:
            self.status = "ambiguous"
        elif status == "ready" and not self.frames:
            self.status = "no_matches"
        else:
            self.status = status
        self.publish()

    def summary(self):
        return {"status": "ambiguous" if self.ambiguous else self.status,
                "frames_received": len(self.frames), "object_id": self.object_id, "revision": self.revision}

    def publish(self):
        write_json(self.directory / "tracking.json", {**self.summary(), "width": self.size[0],
            "height": self.size[1], "frames": [self.frames[index] for index in sorted(self.frames)]})
