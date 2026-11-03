"""Reference-counted Meta uploads shared by Spark and SAM consumers."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock
import time

from openai import OpenAI

from pawception.muse import BASE_URL, safe_error
from pawception.files import write_json

# Cleanup never blocks inference or the monitor lock. One worker across sessions.
_CLEANUP = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pawception-upload-cleanup")


class SharedUpload:
    def __init__(self, path, cancelled, client_factory):
        self.path, self.cancelled, self.client_factory = path, cancelled, client_factory
        self.lock, self.finished = Lock(), Event()
        self.file_id, self.status, self.error = None, "not_uploaded", None
        self.owners = {"spark"}

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _snapshot(self):
        return {"file_id": self.file_id, "status": self.status, "consumers": sorted(self.owners),
                "cleanup_error": self.error}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.path, self._snapshot())
        except OSError:
            # Failed receipt writes must never prevent remote cleanup.
            pass

    def register(self, file_id):
        with self.lock:
            if self.file_id is not None:
                raise ValueError("A window can only register one upload")
            self.file_id, self.status = file_id, "in_use"
            self._save()

    def finish_spark(self, success):
        with self.lock:
            if "spark" not in self.owners:
                return
            self.owners.discard("spark")
            if success and not self.cancelled.is_set() and self.file_id:
                self.owners.add("handoff")
            self._release_if_unused()

    def acquire(self, consumer):
        with self.lock:
            if self.cancelled.is_set() or "handoff" not in self.owners or self.status != "in_use":
                return None
            self.owners.add(consumer)
            self._save()
            return self.file_id

    def release(self, consumer):
        with self.lock:
            self.owners.discard(consumer)
            self._release_if_unused()

    def _release_if_unused(self):
        if not self.owners:
            if not self.file_id:
                self.finished.set()
            elif self.status == "in_use":
                self.status = "cleaning_up"
                _CLEANUP.submit(self._delete)
        self._save()

    def _delete(self):
        try:
            with self.client_factory() as client:
                receipt = client.files.delete(self.file_id)
            status, error = ("deleted" if receipt.deleted else "not_confirmed"), None
        except Exception as failure:
            status, error = "failed", safe_error(failure)
        with self.lock:
            self.status, self.error = status, error
            self._save()
            self.finished.set()


class SharedUploads:
    def __init__(self, directory, api_key, *, client_factory=None):
        self.directory = directory
        self.client_factory = client_factory or (lambda: OpenAI(
            api_key=api_key, base_url=BASE_URL, timeout=30, max_retries=0))
        self.cancelled, self.lock, self.uploads = Event(), Lock(), {}

    def for_window(self, window_id):
        with self.lock:
            if window_id not in self.uploads:
                self.uploads[window_id] = SharedUpload(
                    self.directory / "windows" / window_id / "upload.json", self.cancelled, self.client_factory)
            return self.uploads[window_id]

    def finish_planning(self, windows):
        with self.lock:
            uploads = [self.uploads[w["id"]] for w in windows
                       if w["id"] in self.uploads and w.get("status") in {"completed", "invalid", "error", "cancelled"}]
        for upload in uploads:
            upload.release("handoff")

    def close(self):
        self.cancelled.set()
        with self.lock:
            uploads = list(self.uploads.values())
        for upload in uploads:
            upload.release("handoff")

    def snapshot(self):
        with self.lock:
            return {key: upload.snapshot() for key, upload in self.uploads.items()}

    def wait(self, timeout=30):
        """Bounded cleanup wait for smoke tests and explicit shutdown checks."""
        deadline = time.monotonic() + timeout
        with self.lock:
            uploads = list(self.uploads.values())
        return all(upload.finished.wait(max(0, deadline-time.monotonic())) for upload in uploads)
