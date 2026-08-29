"""HTTP server: MJPEG stream with overlay + JSON detection endpoint + static UI.

A single producer thread grabs → detects → encodes JPEG. HTTP handlers just
copy the latest encoded buffer and wait on a condition variable. This keeps CPU
flat regardless of how many browser tabs are connected, and stops crashes from
racing on shared state.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2

from camera import Camera
from detector import DandruffDetector
from makeup_detector import MakeupDetector

HOST = "0.0.0.0"
PORT = 8080
_DEFAULT_CAM = "0" if sys.platform.startswith("win") else "/dev/video0"
CAM_DEVICE = os.environ.get("MM_CAM", _DEFAULT_CAM)
MODE = os.environ.get("MM_MODE", "dandruff").lower()
# Capture size + FPS come from env so the RDK X5 systemd unit can trim them
# down without editing code. Defaults are Windows-dev-friendly (1080p30);
# the RDK unit sets CAM_W=1280 CAM_H=720 TARGET_FPS=15 to fit the 3 GB board.
CAM_W = int(os.environ.get("MM_CAM_W", "1920"))
CAM_H = int(os.environ.get("MM_CAM_H", "1080"))
CAM_FPS = int(os.environ.get("MM_CAM_FPS", "30"))
TARGET_FPS = int(os.environ.get("MM_TARGET_FPS", "25"))
# Run the detector every Nth captured frame; other frames re-use the last
# result and get a fresh overlay drawn. Keeps CPU flat when face is present.
DETECT_EVERY = int(os.environ.get("MM_DETECT_EVERY", "1"))
JPEG_QUALITY = int(os.environ.get("MM_JPEG_QUALITY", "80"))

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")

_camera = Camera(device=CAM_DEVICE, width=CAM_W, height=CAM_H, fps=CAM_FPS)
if MODE == "makeup":
    _detector = MakeupDetector()
    _INDEX_PAGE = "makeup.html"
else:
    _detector = DandruffDetector()
    _INDEX_PAGE = "index.html"

# Shared latest encoded frame + detection result.
_state_lock = threading.Lock()
_state_cv = threading.Condition(_state_lock)
_latest_jpeg: bytes | None = None
_latest_seq = 0
_latest_result: dict = {"count": 0, "detections": [], "hair_ratio": 0.0, "ts": 0.0}


def _producer() -> None:
    global _latest_jpeg, _latest_seq
    period = 1.0 / TARGET_FPS
    frame_i = 0
    last_result = {"count": 0, "detections": [], "hair_ratio": 0.0}
    while True:
        t0 = time.time()
        try:
            frame = _camera.read()
            if frame is None:
                time.sleep(0.05)
                continue
            # Detector is the CPU-heavy step — throttle it, reuse last result
            # on the frames we skip. Overlay is cheap and still redraws.
            if frame_i % DETECT_EVERY == 0:
                last_result = _detector.detect(frame)
            frame_i += 1
            result = last_result
            result["ts"] = time.time()
            annotated = _detector.draw(frame, result)
            ok, buf = cv2.imencode(".jpg", annotated,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
            if not ok:
                continue
            with _state_cv:
                _latest_jpeg = buf.tobytes()
                _latest_seq += 1
                _latest_result.update(result)
                _state_cv.notify_all()
        except Exception:
            traceback.print_exc()
            time.sleep(0.1)
        # Pace the loop.
        dt = time.time() - t0
        if dt < period:
            time.sleep(period - dt)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):  # noqa: A002
        pass

    def do_GET(self):
        try:
            if self.path == "/stream.mjpg":
                self._serve_stream()
            elif self.path == "/detections.json":
                self._serve_json()
            elif self.path in ("/", "/index.html"):
                self._serve_static(_INDEX_PAGE, "text/html; charset=utf-8")
            elif self.path.startswith("/") and ".." not in self.path:
                name = self.path.lstrip("/")
                mime = {
                    ".css": "text/css",
                    ".js": "application/javascript",
                    ".html": "text/html; charset=utf-8",
                }.get(os.path.splitext(name)[1], "application/octet-stream")
                self._serve_static(name, mime)
            else:
                self.send_error(404)
        except (OSError, ConnectionError):
            # Client disconnected mid-response — expected on tab close / refresh.
            return
        except Exception:
            traceback.print_exc()

    def _serve_static(self, name: str, mime: str):
        path = os.path.join(FRONTEND_DIR, name)
        if not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_json(self):
        with _state_lock:
            payload = json.dumps(_latest_result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _serve_stream(self):
        boundary = "frame"
        self.send_response(200)
        self.send_header(
            "Content-Type", f"multipart/x-mixed-replace; boundary={boundary}"
        )
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last_seq = -1
        while True:
            with _state_cv:
                while _latest_seq == last_seq or _latest_jpeg is None:
                    if not _state_cv.wait(timeout=2.0):
                        break  # keep-alive check; loop will re-check state
                if _latest_jpeg is None:
                    continue
                jpg = _latest_jpeg
                last_seq = _latest_seq
            head = (
                f"--{boundary}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(jpg)}\r\n\r\n"
            ).encode()
            self.wfile.write(head)
            self.wfile.write(jpg)
            self.wfile.write(b"\r\n")


def main() -> None:
    _camera.start()
    threading.Thread(target=_producer, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[make-up-mirror] listening on http://{HOST}:{PORT}  cam={CAM_DEVICE}  mode={MODE}")
    try:
        server.serve_forever()
    finally:
        _camera.stop()


if __name__ == "__main__":
    main()
