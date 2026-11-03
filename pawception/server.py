"""Serve the monitor and developer lab. Muse mode makes explicitly selected API calls."""

import argparse
import json
import re
from uuid import uuid4
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from subprocess import SubprocessError
from threading import Lock

from pawception.replay import build_replay
from pawception.experiments import experiment_detail, experiment_index, media_assets
from pawception.pipeline_store import pipeline_index, pipeline_detail, pipeline_media, pipeline_window
from pawception.live_mock import LiveMonitor, MODELS
from pawception.safety import DEFAULT_HAZARD_PROMPT
from pawception.offline import Offline
from pawception.labeling import LabelStore

from pawception.paths import ROOT

ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/experiments.js": ("experiments.js", "text/javascript"), "/styles.css": ("styles.css", "text/css"), "/experiments.css": ("experiments.css", "text/css")}
ASSETS.update({"/pipeline.js": ("pipeline.js", "text/javascript"), "/pipeline.css": ("pipeline.css", "text/css")})
ASSETS.update({"/monitor": ("../monitor/index.html", "text/html"),
               "/monitor.css": ("../monitor/monitor.css", "text/css"),
               "/monitor.js": ("../monitor/monitor.js", "text/javascript"),
               "/activity-review.js": ("../monitor/activity-review.js", "text/javascript")})
ASSETS["/webcam.js"] = ("../monitor/webcam.js", "text/javascript")
ASSETS["/event-suggestions.js"] = ("../monitor/event-suggestions.js", "text/javascript")
ASSETS["/muse.svg"] = ("../monitor/muse.svg", "image/svg+xml")
ASSETS["/eye.svg"] = ("../monitor/eye.svg", "image/svg+xml")
ASSETS["/pawception-logo.png"] = ("../monitor/pawception-logo.png", "image/png")
ASSETS["/sam-overlay.js"] = ("../monitor/sam-overlay.js", "text/javascript")
ASSETS.update({'/offline':('../offline/index.html','text/html'),
               '/offline.js':('../offline/offline.js','text/javascript'),
               '/offline.css':('../offline/offline.css','text/css')})
ASSETS.update({f'/spatial.{extension}':(f'../offline/spatial.{extension}', mime)
               for extension,mime in [('html','text/html'),('js','text/javascript'),('css','text/css')]})
ASSETS.update({'/label':('../label/index.html','text/html'),
               '/label.js':('../label/label.js','text/javascript'),
               '/label.css':('../label/label.css','text/css')})
LABELS = LabelStore(ROOT)
MONITOR = LiveMonitor()
OFFLINE = None
OFFLINE_LOCK = Lock()


def offline():
    global OFFLINE
    with OFFLINE_LOCK:
        if OFFLINE is None:
            OFFLINE = Offline(ROOT)
    return OFFLINE


def monitor_sample():
    path = ROOT / "data/demo/generated/online/01_cat_in_can.mp4"
    if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to((ROOT / "data").resolve()):
        return path
    return None


def monitor_source(source_id):
    if source_id == "sample":
        return monitor_sample()
    if not isinstance(source_id, str) or not re.fullmatch(r"[a-f0-9]{32}", source_id):
        return None
    directory = ROOT / "data/live_sources" / source_id
    path = directory / "source.mp4"
    if path.is_file() and not path.is_symlink() and not directory.is_symlink() and directory.resolve().parent == (ROOT / "data/live_sources").resolve():
        return path
    return None


class ReplayHandler(BaseHTTPRequestHandler):
    def respond(self, status, content, mime="application/json"):
        body = json.dumps(content).encode() if mime == "application/json" else content
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def replay(self, settings=None):
        fixture = json.loads((ROOT / "data/fixtures/temporal_demo.json").read_text(encoding="utf-8"))
        return build_replay(fixture, settings)

    def do_GET(self):
        route = urlparse(self.path).path
        if route in ASSETS:
            filename, mime = ASSETS[route]
            self.respond(200, (ROOT / "ui/replay" / filename).read_bytes(), mime)
        elif re.fullmatch(r'/evaluation-reports/submission-\d{8}T\d{6}_\d{6}Z/report\.(html|md|json)',route):
            _,_,identifier,filename=route.split('/')
            folder=ROOT/'data/evaluations'/identifier
            path=folder/filename
            if path.is_file() and not any(p.is_symlink() for p in [path,folder,folder.parent]):
                mime={'html':'text/html','md':'text/markdown','json':'application/json'}[filename.rsplit('.',1)[1]]
                self.respond(200,json.loads(path.read_text()) if mime=='application/json' else path.read_bytes(),mime)
            else:self.respond(404,{'error':'Evaluation report unavailable'})
        elif route in {'/api/labels','/api/labels/export'}:
            try:
                self.respond(200, LABELS.load() if route.endswith('/export') else LABELS.snapshot())
            except (ValueError,OSError) as error:
                self.respond(400, {'error':str(error)})
        elif re.fullmatch(r'/label-media/[a-zA-Z0-9_]+\.mp4',route):
            try:
                self.serve_video('',path=LABELS.media(route.rsplit('/',1)[1][:-4]))
            except (ValueError,OSError) as error:
                self.respond(404, {'error':str(error)})
        elif route == '/api/offline':
            self.respond(200, offline().index())
        elif re.fullmatch(r'/api/offline/[a-f0-9]{32}/spatial', route):
            try:
                self.respond(200, offline().spatial.status(route.split('/')[3]))
            except (ValueError, OSError) as error:
                self.respond(400, {'error':str(error)})
        elif re.fullmatch(r'/offline-results/[a-f0-9]{32}/spatial/[a-f0-9]{32}/(scene.json|background.png)', route):
            try:
                parts = route.split('/')
                self.respond(200, offline().spatial.asset(parts[2], parts[4], parts[5]).read_bytes(),
                             'image/png' if parts[5].endswith('.png') else 'application/octet-stream')
            except (ValueError, OSError) as error:
                self.respond(404, {'error':str(error)})
        elif re.fullmatch(r'/api/offline/[a-f0-9]{32}', route):
            try:
                query = parse_qs(urlparse(self.path).query)
                self.respond(200, offline().snapshot(route.rsplit('/',1)[1],
                    start=float(query['start'][0]) if 'start' in query else None,
                    end=float(query['end'][0]) if 'end' in query else None,
                    cell_size=int(query['cell_size'][0]) if 'cell_size' in query else None))
            except (ValueError, OSError) as error:
                self.respond(400, {'error':str(error)})
        elif re.fullmatch(r'/offline-results/[a-f0-9]{32}/background.png', route):
            path = offline().background_image(route.split('/')[2])
            if path:
                self.respond(200,path.read_bytes(),'image/png')
            else:
                self.respond(404,{'error':'Background image unavailable for this analysis'})
        elif re.fullmatch(r'/offline-results/[a-f0-9]{32}/changes/[0-9]{6}\.png', route):
            parts=route.split('/')
            path=offline().change_frame_image(parts[2],int(parts[4][:-4]))
            if path:self.respond(200,path.read_bytes(),'image/png')
            else:self.respond(404,{'error':'Change-detection frame unavailable'})
        elif re.fullmatch(r'/offline-results/[a-f0-9]{32}/sam/[0-9]{4}/[0-9]{6}\.png', route):
            parts=route.split('/')
            path=offline().sam_frame_image(parts[2],parts[4],int(parts[5][:-4]))
            if path:
                self.respond(200,path.read_bytes(),'image/png')
            else:
                self.respond(404,{'error':'SAM frame unavailable'})
        elif re.fullmatch(r'/offline-media/[a-zA-Z0-9_]+/(video.mp4|frame.jpg)', route):
            try:
                identifier = route.split('/')[2]
                if route.endswith('frame.jpg'):
                    query = parse_qs(urlparse(self.path).query)
                    timestamp = float(query['t'][0]) if 't' in query else None
                    self.respond(200, offline().preview(identifier,timestamp).read_bytes(), 'image/jpeg')
                else:
                    self.serve_video('', path=offline().source(identifier)[1])
            except (ValueError, OSError, SubprocessError):
                self.respond(404, {'error':'Recording preview unavailable'})
        elif route == "/api/replay":
            self.respond(200, self.replay())
        elif route == "/api/experiments":
            self.respond(200, experiment_index(ROOT))
        elif route == "/api/pipeline":
            self.respond(200, pipeline_index(ROOT))
        elif route == "/api/monitor":
            self.respond(200, {"models": MODELS, "sample_url": "/monitor-media/sample.mp4" if monitor_sample() else None,
                               "safety_defaults": {"safety_prompt": DEFAULT_HAZARD_PROMPT, "safety_exclusions": ""},
                               "session": MONITOR.current()})
        elif re.fullmatch(r"/api/monitor/[a-f0-9]{32}", route):
            try:
                self.respond(200, MONITOR.update(route.rsplit("/", 1)[1]))
            except ValueError as error:
                self.respond(404, {"error": str(error)})
        elif route == "/monitor-media/sample.mp4":
            self.serve_video("couch", monitor=True)
        elif re.fullmatch(r"/alert-images/[a-f0-9]{32}/[a-f0-9]{24}/((original|zoom|highlighted)\.jpg|snapshots/[0-9]{6}/(original|zoom|highlighted)\.jpg|pet/tracking\.json|pet/frames/[0-9]{6}\.png)", route):
            _, _, session_id, alert_id, *parts = route.split("/")
            filename = "/".join(parts)
            path = ROOT / "data/live_sessions" / session_id / "evidence" / alert_id / filename
            base = (ROOT / "data/live_sessions").resolve()
            if path.is_file() and path.resolve().is_relative_to(base) and not any(p.is_symlink() for p in [path, *path.parents] if p != base and p.is_relative_to(base)):
                mime = "application/json" if filename.endswith(".json") else "image/png" if filename.endswith(".png") else "image/jpeg"
                content = json.loads(path.read_text()) if mime == "application/json" else path.read_bytes()
                self.respond(200, content, mime)
            else:
                self.respond(404, {"error": "Alert image not available"})
        elif re.fullmatch(r"/webcam-media/[a-f0-9]{32}/(clip.mp4|frame.jpg)", route):
            try:
                query = parse_qs(urlparse(self.path).query)
                path = MONITOR.camera_media(route.split("/")[2], start=float(query["start"][0]),
                                            end=float(query["end"][0]), frame=route.endswith(".jpg"))
                if route.endswith(".jpg"):
                    self.respond(200, path.read_bytes(), "image/jpeg")
                else:
                    self.serve_video("", path=path)
            except (KeyError, ValueError, OSError, SubprocessError):
                self.respond(400, {"error": "This camera interval is not available yet"})
        elif route.startswith("/api/pipeline/"):
            parts = route.removeprefix("/api/pipeline/").split("/")
            if len(parts) == 1:
                detail = pipeline_detail(ROOT, parts[0])
            elif len(parts) == 3 and parts[1] == "windows":
                detail = pipeline_window(ROOT, parts[0], parts[2])
            else:
                detail = None
            self.respond(200, detail) if detail else self.respond(404, {"error": "Pipeline run not found"})
        elif route.startswith("/api/experiments/"):
            detail = experiment_detail(ROOT, route.removeprefix("/api/experiments/"))
            self.respond(200, detail) if detail else self.respond(404, {"error": "Run not found"})
        elif route.startswith("/media/"):
            self.serve_video(route.removeprefix("/media/"))
        elif route.startswith("/pipeline-media/"):
            self.serve_video(route.removeprefix("/pipeline-media/"), pipeline=True)
        elif route.startswith("/previews/"):
            filename = route.removeprefix("/previews/")
            metadata = json.loads((ROOT / "data/video_metadata.json").read_text())
            allowed = {Path(frame).name for clip in metadata for frame in clip.get("decoded_preview_frames", [])}
            path = ROOT / "data/previews" / filename
            if filename in allowed and not path.is_symlink() and path.is_file():
                self.respond(200, path.read_bytes(), "image/jpeg")
            else:
                self.respond(404, {"error": "Preview not found"})
        else:
            self.respond(404, {"error": "Not found"})

    def serve_video(self, filename, *, pipeline=False, monitor=False, path=None):
        if path is not None:
            pass
        elif monitor:
            path = monitor_sample()
        elif pipeline:
            parts = filename.split("/")
            path = pipeline_media(ROOT, *parts) if len(parts) == 2 else None
        else:
            path = media_assets(ROOT).get(filename)
        if path is None:
            self.respond(404, {"error": "Video not found"})
            return
        size = path.stat().st_size
        start, end = 0, size - 1
        request_range = self.headers.get("Range")
        if request_range:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", request_range)
            if match and any(match.groups()):
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(int(right), end) if right else end
                else:
                    start = max(0, size - int(right))
            else:
                start = size
            if not 0 <= start <= end < size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        self.send_response(206 if request_range else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        if request_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with path.open("rb") as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = stream.read(min(remaining, 256 * 1024))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # Browser seeking or pausing can cancel an in-flight range.

    def do_POST(self):
        route = urlparse(self.path).path
        if re.fullmatch(r'/api/labels/[a-zA-Z0-9_]+',route):
            try:
                size = int(self.headers.get('Content-Length','0'))
                if self.headers.get_content_type()!='application/json' or not 0<size<=131072:
                    raise ValueError('Expected a small JSON annotation request')
                value = json.loads(self.rfile.read(size))
                if not isinstance(value,dict):
                    raise ValueError('Expected an annotation object')
                self.respond(200,LABELS.save_clip(route.rsplit('/',1)[1],value))
            except (ValueError,TypeError,OSError) as error:
                self.respond(400,{'error':str(error)})
            return
        if route == '/api/offline/source':
            directory = None
            try:
                from urllib.parse import unquote
                size = int(self.headers.get('Content-Length','0'))
                if not 0 < size <= 512*1024*1024:
                    raise ValueError('Choose a recording up to 512 MB')
                identifier = uuid4().hex
                target_directory = ROOT / 'data/offline_sources' / identifier
                target_directory.mkdir(parents=True)
                directory = target_directory
                path = directory / 'source.mp4'
                with path.open('xb') as destination:
                    remaining = size
                    while remaining:
                        chunk = self.rfile.read(min(remaining,256*1024))
                        if not chunk:
                            raise ValueError('Recording upload was interrupted')
                        destination.write(chunk)
                        remaining -= len(chunk)
                self.respond(200, offline().register(path,identifier,unquote(self.headers.get('X-Filename','Saved recording'))))
            except (ValueError,OSError,SubprocessError):
                if directory:
                    import shutil
                    shutil.rmtree(directory)
                self.respond(400, {'error':'Could not load the recording. Choose a playable video up to 512 MB and eight hours.'})
            return
        if route == '/api/offline/runs' or re.fullmatch(r'/api/offline/[a-f0-9]{32}/(stop|spatial|spatial/stop)',route):
            try:
                size = int(self.headers.get('Content-Length','0'))
                if self.headers.get_content_type() != 'application/json' or not 0 < size <= 32_768:
                    raise ValueError('Expected a small JSON analysis request')
                value = json.loads(self.rfile.read(size))
                if not isinstance(value,dict):
                    raise ValueError('Expected analysis settings')
                service = offline()
                if '/spatial' in route:
                    identifier = route.split('/')[3]
                    if identifier not in service.jobs:
                        raise ValueError('Analysis not found')
                    if route.endswith('/stop'):
                        result = service.spatial.stop(identifier)
                    else:
                        if any(job['status'] in {'queued','running','stopping'} for job in service.jobs.values()):
                            raise ValueError('Wait for the current recording analysis to finish first.')
                        result = service.spatial.start(service.jobs[identifier],service.source(service.jobs[identifier]['source_id'])[1],value)
                else:
                    result = service.create(value) if route.endswith('/runs') else service.stop(route.split('/')[3])
                self.respond(200,result)
            except (ValueError,TypeError,OSError) as error:
                self.respond(400,{'error':str(error)})
            return
        if self.path in {"/api/monitor/suggestions", "/api/monitor/route"}:
            from pawception.event_suggestions import SuggestionError, suggest_descriptions
            try:
                if self.headers.get_content_type() != "application/json":
                    self.respond(415, {"error": "Expected application/json"})
                    return
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ValueError("Expected a small event draft")
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict):
                    raise ValueError("Expected an event draft")
                if self.path.endswith("/route"):
                    from pawception.event_inputs import route_event
                    self.respond(200, route_event(value, root=ROOT))
                else:
                    self.respond(200, suggest_descriptions(value, root=ROOT))
            except (ValueError, TypeError) as error:
                self.respond(400, {"error": str(error)})
            except SuggestionError as error:
                self.respond(502, {"error": str(error)})
            return
        if re.fullmatch(r"/api/monitor/[a-f0-9]{32}/camera", self.path):
            try:
                from pawception.live_webcam import MAX_CHUNK_BYTES
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_CHUNK_BYTES:
                    raise ValueError("Camera chunk is too large")
                data = self.rfile.read(size)
                if len(data) != size:
                    raise ValueError("Camera upload was interrupted")
                state = MONITOR.camera_chunk(self.path.split("/")[3], data,
                    sequence=int(self.headers.get("X-Capture-Sequence", "-1")),
                    end=float(self.headers.get("X-Capture-End", "nan")),
                    mime=self.headers.get_content_type())
                self.respond(200, state)
            except (ValueError, OSError) as error:
                self.respond(400, {"error": str(error)})
            return
        if self.path == "/api/monitor/source":
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 200 * 1024 * 1024:
                    raise ValueError("Choose a video up to 200 MB for Muse mode")
                source_id = uuid4().hex
                directory = ROOT / "data/live_sources" / source_id
                directory.mkdir(parents=True)
                path = directory / "source.mp4"
                with path.open("xb") as destination:
                    remaining = size
                    while remaining:
                        chunk = self.rfile.read(min(remaining, 256 * 1024))
                        if not chunk:
                            raise ValueError("Video upload was interrupted")
                        destination.write(chunk)
                        remaining -= len(chunk)
                self.respond(200, {"source_id": source_id})
            except (ValueError, OSError) as error:
                self.respond(400, {"error": str(error)})
            return
        if self.path == "/api/monitor" or re.fullmatch(r"/api/monitor/[a-f0-9]{32}/(event|stop|viewed|safety)", self.path):
            if self.headers.get_content_type() != "application/json":
                self.respond(415, {"error": "Expected application/json"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32_768:
                    raise ValueError("Expected a small JSON settings object")
                value = json.loads(self.rfile.read(size))
                if not isinstance(value, dict):
                    raise ValueError("Expected a JSON object")
                if self.path == "/api/monitor":
                    source = monitor_source(value.get("source_id")) if value.get("provider") == "muse" else None
                    state = MONITOR.create(value, root=ROOT, source=source)
                else:
                    _, _, _, session_id, action = self.path.split("/")
                    state = MONITOR.update(session_id, action, value)
                self.respond(200, state)
            except (ValueError, TypeError) as error:
                self.respond(400, {"error": str(error)})
            return
        if self.path != "/api/replay":
            self.respond(404, {"error": "Not found"})
            return
        if self.headers.get_content_type() != "application/json":
            self.respond(415, {"error": "Expected application/json"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                raise ValueError("Expected a small JSON settings object")
            settings = json.loads(self.rfile.read(size))
            self.respond(200, self.replay(settings))
        except (ValueError, TypeError) as error:
            self.respond(400, {"error": str(error)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), ReplayHandler) as server:
        print(f"Pawception monitor: http://127.0.0.1:{server.server_port}/monitor", flush=True)
        print(f"Developer lab: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
