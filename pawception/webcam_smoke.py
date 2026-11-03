"""Exercise the real browser recorder with an animated canvas, without a camera.

Run `uv run --locked python -m scripts.webcam_browser_smoke`, then open
http://127.0.0.1:8766/monitor and select Mock -> Webcam -> Enable camera.
This separate server/session does not affect the normal app on port 8765.
"""

import argparse
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from pawception.server import ReplayHandler
from pawception.paths import ROOT


SYNTHETIC_CAMERA = """
<script>
// Substitute only the device. MediaRecorder, HTTP uploads, encoding, replay,
// event merging, and alert review all use the production implementation.
navigator.mediaDevices.getUserMedia = async () => {
  const canvas = document.createElement('canvas');
  canvas.width = 640; canvas.height = 480;
  const context = canvas.getContext('2d');
  let frame = 0;
  const draw = () => {
    context.fillStyle = '#e9efdf'; context.fillRect(0,0,640,480);
    context.fillStyle = '#46603b'; context.font = '28px sans-serif';
    context.fillText('Synthetic camera test',60,80);
    context.fillText(`Frame ${frame++}`,60,125);
    context.fillRect(60+frame%450,220,70,70);
  };
  draw();
  const timer = setInterval(draw,100);
  const stream = canvas.captureStream(10);
  const track = stream.getVideoTracks()[0];
  const stop = track.stop.bind(track);
  track.stop = () => { clearInterval(timer); stop(); };
  return stream;
};
document.addEventListener('DOMContentLoaded', () => {
  document.title += ' · Synthetic camera test';
  document.getElementById('setup-title').textContent = 'Synthetic camera test';
});
</script>
"""


class SmokeHandler(ReplayHandler):
    def do_GET(self):
        if urlparse(self.path).path == "/monitor":
            page = (ROOT / "ui/monitor/index.html").read_text()
            page = page.replace('<script src="/webcam.js"', SYNTHETIC_CAMERA + '<script src="/webcam.js"', 1)
            self.respond(200, page.encode(), "text/html")
        else:
            super().do_GET()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with ThreadingHTTPServer(("127.0.0.1", args.port), SmokeHandler) as server:
        print(f"Synthetic camera test: http://127.0.0.1:{server.server_port}/monitor", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
