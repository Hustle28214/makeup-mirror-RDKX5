"""USB camera face tracking with SPEED-controlled servos on PCA9685.

Servo layout (PCA9685 on /dev/i2c-5, addr 0x40):
  CH0 = servo1 = base yaw (rotate around Z, camera pans left/right)
  CH1 = servo2 = tilt     (rotate around Y, camera looks up/down)

Control law (both servos behave like continuous rotation with stop@1575us):
  pulse_us = stop_us + gain * pixel_error_from_frame_center
  Face centered -> error 0 -> pulse = stop -> servo halts.
"""
import argparse
import os
import signal
import sys
import threading
import time

# Prefer system-packaged OpenCV 4.5.4 (has V4L2 backend) over pip 4.11 wheel.
sys.path = [p for p in sys.path
            if "/usr/local/lib/python3.10/dist-packages" not in p]
if "/usr/lib/python3/dist-packages" not in sys.path:
    sys.path.insert(0, "/usr/lib/python3/dist-packages")

import cv2  # noqa: E402

from pca9685 import PCA9685  # noqa: E402


def build_argparser():
    p = argparse.ArgumentParser()
    p.add_argument("--cam", type=int, default=0)
    p.add_argument("--width", type=int, default=640)
    p.add_argument("--height", type=int, default=480)
    p.add_argument("--fps", type=int, default=30)

    p.add_argument("--i2c-bus", type=int, default=5)
    p.add_argument("--addr", type=lambda s: int(s, 0), default=0x40)
    p.add_argument("--yaw-ch", type=int, default=0)
    p.add_argument("--tilt-ch", type=int, default=1)

    p.add_argument("--stop-us", type=int, default=1575)
    p.add_argument("--yaw-stop-us", type=int, default=None)
    p.add_argument("--tilt-stop-us", type=int, default=None)
    p.add_argument("--max-speed-us", type=int, default=250)
    p.add_argument("--yaw-gain", type=float, default=1.6)
    p.add_argument("--tilt-gain", type=float, default=1.6)
    p.add_argument("--yaw-invert", action="store_true")
    p.add_argument("--tilt-invert", action="store_true")
    p.add_argument("--deadband-px", type=int, default=25)
    p.add_argument("--min-speed-us", type=int, default=25)

    p.add_argument("--lost-frames", type=int, default=6)
    p.add_argument("--startup-park", type=float, default=0.4)
    p.add_argument("--min-face", type=int, default=40,
                   help="Minimum face size in pixels for detection (smaller = detects farther faces)")

    p.add_argument("--show", action="store_true",
                   help="Local OpenCV preview window (needs DISPLAY)")
    p.add_argument("--web-port", type=int, default=None,
                   help="Serve MJPEG debug UI on this port (e.g. 8080)")
    p.add_argument("--no-servo", action="store_true",
                   help="Skip PCA9685, detect only")
    return p


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def compute_pulse(err_px, stop_us, gain, invert, deadband_px,
                  min_speed_us, max_speed_us):
    if abs(err_px) <= deadband_px:
        return stop_us
    delta = gain * err_px
    if invert:
        delta = -delta
    delta = clamp(delta, -max_speed_us, max_speed_us)
    if 0 < abs(delta) < min_speed_us:
        delta = min_speed_us if delta > 0 else -min_speed_us
    return int(round(stop_us + delta))


# -------------------- web streaming --------------------

_state_lock = threading.Lock()
_state = {"jpeg": None, "info": ""}
_web_stop = threading.Event()


def _update_state(jpeg_bytes, info_text):
    with _state_lock:
        _state["jpeg"] = jpeg_bytes
        _state["info"] = info_text


def _get_state():
    with _state_lock:
        return _state["jpeg"], _state["info"]


INDEX_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>face-track debug</title>
<style>
  body{margin:0;background:#111;color:#eee;font:14px/1.4 monospace;text-align:center}
  h1{margin:8px 0;font-size:14px;color:#8ff}
  img{max-width:100%;height:auto;image-rendering:auto}
  #info{padding:8px 12px;color:#8f8;white-space:pre-wrap;text-align:left;max-width:720px;margin:0 auto}
</style></head><body>
<h1>face-track debug (RDK X5 + PCA9685 + MG90S)</h1>
<img src="/stream">
<pre id="info">loading...</pre>
<script>
setInterval(()=>fetch('/info').then(r=>r.text()).then(t=>{document.getElementById('info').textContent=t}),200);
</script>
</body></html>
"""


def _start_web_server(port):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw):
            return  # silence

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(INDEX_HTML)))
                self.end_headers()
                self.wfile.write(INDEX_HTML)
                return
            if self.path == "/info":
                _, info = _get_state()
                body = info.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path == "/stream":
                self.send_response(200)
                self.send_header("Age", "0")
                self.send_header("Cache-Control", "no-cache, private")
                self.send_header("Pragma", "no-cache")
                self.send_header("Content-Type",
                                 "multipart/x-mixed-replace; boundary=FRAME")
                self.end_headers()
                try:
                    while not _web_stop.is_set():
                        jpeg, _ = _get_state()
                        if jpeg is not None:
                            self.wfile.write(b"--FRAME\r\n")
                            self.wfile.write(b"Content-Type: image/jpeg\r\n")
                            self.wfile.write(
                                b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n")
                            self.wfile.write(jpeg)
                            self.wfile.write(b"\r\n")
                        time.sleep(0.04)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            self.send_response(404)
            self.end_headers()

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# -------------------- main --------------------

def main():
    args = build_argparser().parse_args()

    yaw_stop = args.yaw_stop_us if args.yaw_stop_us is not None else args.stop_us
    tilt_stop = args.tilt_stop_us if args.tilt_stop_us is not None else args.stop_us

    cascade_path = None
    profile_path = None
    for base in (
        "/usr/share/opencv4/haarcascades",
        "/usr/local/lib/python3.10/dist-packages/cv2/data",
    ):
        cand_f = os.path.join(base, "haarcascade_frontalface_default.xml")
        cand_p = os.path.join(base, "haarcascade_profileface.xml")
        if os.path.exists(cand_f):
            cascade_path = cand_f
            if os.path.exists(cand_p):
                profile_path = cand_p
            break
    if cascade_path is None:
        print("[ERR] no haarcascade_frontalface_default.xml found", file=sys.stderr)
        return 2
    face_cascade = cv2.CascadeClassifier(cascade_path)
    if face_cascade.empty():
        print(f"[ERR] failed to load {cascade_path}", file=sys.stderr)
        return 2
    profile_cascade = None
    if profile_path:
        profile_cascade = cv2.CascadeClassifier(profile_path)
        if profile_cascade.empty():
            profile_cascade = None
    print(f"[det] frontal={cascade_path}")
    if profile_cascade:
        print(f"[det] profile={profile_path}")

    cap = cv2.VideoCapture(args.cam, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, args.fps)
    if not cap.isOpened():
        print(f"[ERR] cannot open /dev/video{args.cam}", file=sys.stderr)
        return 2

    pca = None
    if not args.no_servo:
        pca = PCA9685(bus=args.i2c_bus, address=args.addr, freq_hz=50)
        pca.set_pulse_us(args.yaw_ch, yaw_stop)
        pca.set_pulse_us(args.tilt_ch, tilt_stop)
        time.sleep(args.startup_park)
        print(f"[init] yaw stop={yaw_stop}us  tilt stop={tilt_stop}us")

    server = None
    if args.web_port:
        server = _start_web_server(args.web_port)
        print(f"[web] http://<board-ip>:{args.web_port}/")

    stop = [False]

    def _sig(_signo, _frame):
        stop[0] = True
        _web_stop.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    cx_img = args.width // 2
    cy_img = args.height // 2
    lost = 0
    frames = 0
    last_log = time.time()
    running_fps = 0.0

    try:
        while not stop[0]:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.01)
                continue
            frames += 1

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            faces = face_cascade.detectMultiScale(
                gray, scaleFactor=1.15, minNeighbors=4,
                minSize=(args.min_face, args.min_face),
                flags=cv2.CASCADE_SCALE_IMAGE,
            )
            det_kind = "frontal" if len(faces) else "-"
            # Frontal missed -> try profile (right-facing), then flip for left-facing
            if len(faces) == 0 and profile_cascade is not None:
                prof = profile_cascade.detectMultiScale(
                    gray, scaleFactor=1.15, minNeighbors=4,
                    minSize=(args.min_face, args.min_face),
                    flags=cv2.CASCADE_SCALE_IMAGE,
                )
                if len(prof):
                    faces = prof
                    det_kind = "profile-R"
                else:
                    flipped = cv2.flip(gray, 1)
                    prof = profile_cascade.detectMultiScale(
                        flipped, scaleFactor=1.15, minNeighbors=4,
                        minSize=(args.min_face, args.min_face),
                        flags=cv2.CASCADE_SCALE_IMAGE,
                    )
                    if len(prof):
                        # un-flip x coordinates back to original image space
                        w_img = gray.shape[1]
                        faces = [(w_img - x - w, y, w, h) for (x, y, w, h) in prof]
                        det_kind = "profile-L"

            face = None
            if len(faces) > 0:
                face = max(faces, key=lambda r: r[2] * r[3])

            err_x = err_y = 0
            if face is not None:
                lost = 0
                x, y, w, h = face
                fx = x + w // 2
                fy = y + h // 2
                err_x = fx - cx_img
                err_y = fy - cy_img

                yaw_us = compute_pulse(err_x, yaw_stop, args.yaw_gain,
                                       args.yaw_invert, args.deadband_px,
                                       args.min_speed_us, args.max_speed_us)
                tilt_us = compute_pulse(err_y, tilt_stop, args.tilt_gain,
                                        args.tilt_invert, args.deadband_px,
                                        args.min_speed_us, args.max_speed_us)

                if pca:
                    pca.set_pulse_us(args.yaw_ch, yaw_us)
                    pca.set_pulse_us(args.tilt_ch, tilt_us)

                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                cv2.drawMarker(frame, (fx, fy), (0, 255, 0),
                               cv2.MARKER_CROSS, 20, 2)
                cv2.line(frame, (cx_img, cy_img), (fx, fy), (0, 200, 0), 1)
            else:
                lost += 1
                yaw_us = yaw_stop
                tilt_us = tilt_stop
                if lost >= args.lost_frames and pca:
                    pca.set_pulse_us(args.yaw_ch, yaw_stop)
                    pca.set_pulse_us(args.tilt_ch, tilt_stop)

            cv2.drawMarker(frame, (cx_img, cy_img), (0, 0, 255),
                           cv2.MARKER_CROSS, 20, 2)
            hud = (f"yaw={yaw_us}us  tilt={tilt_us}us  "
                   f"err=({err_x:+d},{err_y:+d})px  "
                   f"faces={len(faces)}[{det_kind}]  lost={lost}  "
                   f"fps={running_fps:.1f}")
            cv2.rectangle(frame, (0, 0), (args.width, 28), (0, 0, 0), -1)
            cv2.putText(frame, hud, (8, 20), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 255, 255), 1, cv2.LINE_AA)

            if args.show:
                cv2.imshow("face-track", frame)
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break

            if server is not None:
                ok, buf = cv2.imencode(
                    ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                if ok:
                    info = (
                        f"faces={len(faces)}  lost={lost}\n"
                        f"err_x={err_x:+d}  err_y={err_y:+d}  (deadband=+/-{args.deadband_px})\n"
                        f"yaw_us={yaw_us}  tilt_us={tilt_us}  stop={yaw_stop}/{tilt_stop}\n"
                        f"yaw_gain={args.yaw_gain}  tilt_gain={args.tilt_gain}  "
                        f"max_speed={args.max_speed_us}us  min_speed={args.min_speed_us}us\n"
                        f"yaw_invert={args.yaw_invert}  tilt_invert={args.tilt_invert}\n"
                        f"fps={running_fps:.1f}"
                    )
                    _update_state(buf.tobytes(), info)

            now = time.time()
            if now - last_log > 1.0:
                dt = now - last_log
                running_fps = frames / dt
                print(f"fps={running_fps:5.1f} faces={len(faces)} "
                      f"err=({err_x:+d},{err_y:+d}) "
                      f"yaw={yaw_us}us tilt={tilt_us}us lost={lost}",
                      flush=True)
                frames = 0
                last_log = now
    finally:
        _web_stop.set()
        if server is not None:
            server.shutdown()
        if pca:
            pca.set_pulse_us(args.yaw_ch, yaw_stop)
            pca.set_pulse_us(args.tilt_ch, tilt_stop)
            time.sleep(0.1)
            pca.release(args.yaw_ch)
            pca.release(args.tilt_ch)
            pca.close()
        cap.release()
        if args.show:
            cv2.destroyAllWindows()

    return 0


if __name__ == "__main__":
    sys.exit(main())
