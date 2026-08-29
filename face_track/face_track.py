"""USB camera face tracking with SPEED-controlled servos on PCA9685.

Servo layout (PCA9685 on /dev/i2c-5, addr 0x40):
  CH0 = servo1 = base yaw (rotate around Z, camera pans left/right)
  CH1 = servo2 = tilt     (rotate around Y, camera looks up/down)

Continuous-rotation servos on this rig (loaded, calibrated 2026-08-30):
  stop_us = 1560   (deadband roughly [1502, 1618] no-load, wider under load)
  yaw:  omega_deg_s ~= 1.86 * Δus - 62,  min |Δus|=60 to start moving
  tilt: omega_deg_s ~= 1.76 * Δus - 64,  min |Δus|=90 to start moving
  direction (as-viewed from operator, camera end):
    pulse > stop -> CCW  ->  yaw pans LEFT / tilt looks DOWN
    pulse < stop -> CW   ->  yaw pans RIGHT / tilt looks UP
  yaw needs --yaw-invert (default on): err_x>0 (face right) => want RIGHT pan.

Position estimate (open-loop integration; no encoder):
  theta_yaw + = LEFT (CCW)   theta_tilt + = DOWN (CCW)
  Auto-zeroed at startup: user parks the mechanism at home pose (camera
  parallel to line of sight, yaw arm perpendicular to mirror edge) BEFORE
  launching. Software limits then clamp Δus toward stop at the bounds.

Control law:
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
    p.add_argument("--width", type=int, default=480)
    p.add_argument("--height", type=int, default=360)
    p.add_argument("--fps", type=int, default=30)
    p.add_argument("--flush-frames", type=int, default=1,
                   help="Grab and discard this many buffered frames per iter "
                        "to keep latency low. 0 disables.")

    p.add_argument("--i2c-bus", type=int, default=5)
    p.add_argument("--addr", type=lambda s: int(s, 0), default=0x40)
    p.add_argument("--yaw-ch", type=int, default=0)
    p.add_argument("--tilt-ch", type=int, default=1)

    # Calibrated 2026-08-30 under load. yaw & tilt speed curves differ:
    #   yaw  omega = 1.86*Δus - 62 (min |Δus|=60us)
    #   tilt omega = 1.76*Δus - 64 (min |Δus|=90us)
    p.add_argument("--stop-us", type=int, default=1560)
    p.add_argument("--yaw-stop-us", type=int, default=None)
    p.add_argument("--tilt-stop-us", type=int, default=None)
    p.add_argument("--max-speed-us", type=int, default=120)
    p.add_argument("--yaw-gain", type=float, default=0.5)
    p.add_argument("--tilt-gain", type=float, default=0.45)
    p.add_argument("--yaw-invert", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--tilt-invert", action=argparse.BooleanOptionalAction, default=False)
    p.add_argument("--yaw-deadband-px", type=int, default=70)
    p.add_argument("--tilt-deadband-px", type=int, default=130)
    p.add_argument("--yaw-min-speed-us", type=int, default=60)
    p.add_argument("--tilt-min-speed-us", type=int, default=90)
    # Speed model (omega_deg_s = slope * Δus - intercept, only |Δus|>=min_us):
    p.add_argument("--yaw-speed-slope", type=float, default=1.86)
    p.add_argument("--yaw-speed-intercept", type=float, default=62.0)
    p.add_argument("--tilt-speed-slope", type=float, default=1.76)
    p.add_argument("--tilt-speed-intercept", type=float, default=64.0)
    # Software angle limits (theta_yaw + = LEFT/CCW, theta_tilt + = DOWN/CCW):
    p.add_argument("--yaw-limit-left-deg", type=float, default=90.0)
    p.add_argument("--yaw-limit-right-deg", type=float, default=90.0)
    p.add_argument("--tilt-limit-up-deg", type=float, default=90.0)
    p.add_argument("--tilt-limit-down-deg", type=float, default=30.0)
    # Safety knobs — the open-loop speed model tends to under-integrate the
    # true angle (real servo carries momentum past pulse=stop, and load
    # sometimes pushes ω higher than calibrated). Scale amplifies the
    # integrated theta so the limit triggers earlier; the soft-brake band
    # tapers Δus toward zero as theta approaches the bound.
    p.add_argument("--theta-scale", type=float, default=3.0)
    p.add_argument("--brake-band-deg", type=float, default=45.0)
    p.add_argument("--du-slew-us", type=int, default=35,
                   help="Max change in signed Δus per frame. Rate-limits "
                        "acceleration -> smoother motion, no abrupt jumps.")

    p.add_argument("--lost-frames", type=int, default=6)
    p.add_argument("--startup-park", type=float, default=0.4)
    p.add_argument("--pulse-window-ms", type=int, default=0,
                   help="Servo drive window per frame in ms. 0 = continuous "
                        "drive (pulse rides between frames -> smoothest motion; "
                        "recommended). Positive N = burst mode (drive N ms then "
                        "park to stop; useful only if fps < 4).")
    p.add_argument("--min-face", type=int, default=40,
                   help="Minimum face size in pixels for detection (smaller = detects farther faces)")
    p.add_argument("--det-scale", type=float, default=0.5,
                   help="Downscale factor applied to the frame before Haar "
                        "detection. 0.5 = ~4x faster. Preview stays full-res.")
    p.add_argument("--min-neighbors", type=int, default=5,
                   help="Haar cascade minNeighbors. Higher = fewer false "
                        "positives from bright textureless regions. 4 permissive, "
                        "5 default, 8 strict.")
    p.add_argument("--skin-check", action=argparse.BooleanOptionalAction,
                   default=True,
                   help="Reject candidates whose median YCrCb chroma is not in "
                        "human skin range. Filters out overexposed white walls.")
    p.add_argument("--skin-cr-min", type=int, default=128)
    p.add_argument("--skin-cr-max", type=int, default=180)
    p.add_argument("--skin-cb-min", type=int, default=75)
    p.add_argument("--skin-cb-max", type=int, default=135)

    p.add_argument("--show", action="store_true",
                   help="Local OpenCV preview window (needs DISPLAY)")
    p.add_argument("--web-port", type=int, default=None,
                   help="Serve MJPEG debug UI on this port (e.g. 8080)")
    p.add_argument("--kiosk", action="store_true",
                   help="Kiosk display: MJPEG page shows only the video with "
                        "a center crosshair. No HUD text, no info panel, no "
                        "face bbox / error line. For full-screen portrait use.")
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


def omega_deg_s(delta_us, slope, intercept, min_us):
    """Continuous-rotation speed model: signed deg/s from signed Δus.
    ω = 0 inside deadband (|Δus| < min_us). Symmetric extrapolation for CW.
    """
    if abs(delta_us) < min_us:
        return 0.0
    if delta_us > 0:
        return slope * delta_us - intercept
    return slope * delta_us + intercept


def apply_angle_limit(pulse_us, stop_us, theta, theta_min, theta_max,
                       brake_band_deg=0.0):
    """If theta already outside a bound, clamp pulse toward stop for that
    direction (positive delta pushes theta up; if theta>=max, kill +delta).
    Within brake_band_deg of a bound, taper the outbound delta linearly.
    """
    delta = pulse_us - stop_us
    if theta >= theta_max and delta > 0:
        return stop_us, True
    if theta <= theta_min and delta < 0:
        return stop_us, True
    if brake_band_deg > 0:
        if delta > 0:
            room = theta_max - theta
            if room < brake_band_deg:
                scale = max(0.0, room / brake_band_deg)
                pulse_us = int(round(stop_us + delta * scale))
                return pulse_us, scale < 1.0
        elif delta < 0:
            room = theta - theta_min
            if room < brake_band_deg:
                scale = max(0.0, room / brake_band_deg)
                pulse_us = int(round(stop_us + delta * scale))
                return pulse_us, scale < 1.0
    return pulse_us, False


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

# Bare kiosk page: just the MJPEG stream centered, no chrome, no text.
KIOSK_HTML = b"""<!doctype html>
<html><head><meta charset="utf-8"><title>face-track</title>
<style>
  html,body{margin:0;padding:0;background:#000;overflow:hidden;height:100vh;width:100vw}
  body{display:flex;align-items:center;justify-content:center;cursor:none}
  img{width:100vw;height:100vh;object-fit:contain;display:block}
</style></head><body>
<img src="/stream">
</body></html>
"""


def _start_web_server(port, kiosk=False):
    page = KIOSK_HTML if kiosk else INDEX_HTML
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a, **_kw):
            return  # silence

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
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
    # Keep only the freshest frame in V4L2's queue — otherwise a slow
    # detection loop backs up 4-8 stale frames of latency.
    try:
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    except Exception:
        pass
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
        server = _start_web_server(args.web_port, kiosk=args.kiosk)
        print(f"[web] http://<board-ip>:{args.web_port}/")

    stop = [False]

    def _sig(_signo, _frame):
        stop[0] = True
        _web_stop.set()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    cx_img = args.width // 2
    cy_img = args.height // 2
    real_dims_logged = False
    lost = 0
    frames = 0
    last_log = time.time()
    running_fps = 0.0

    # Position estimate state. Auto-zero at startup: user is responsible for
    # physically parking the mechanism at its home pose (camera axis parallel
    # to line of sight, yaw arm perpendicular to mirror edge) BEFORE launching.
    # theta_yaw + = LEFT (CCW-as-viewed);  theta_tilt + = DOWN (CCW-as-viewed).
    theta_yaw = 0.0
    theta_tilt = 0.0
    # Signed bounds derived from user-facing limit args:
    theta_yaw_max = args.yaw_limit_left_deg      # +LEFT
    theta_yaw_min = -args.yaw_limit_right_deg    # -RIGHT
    theta_tilt_max = args.tilt_limit_down_deg    # +DOWN
    theta_tilt_min = -args.tilt_limit_up_deg     # -UP
    pulse_window_s = max(0.0, args.pulse_window_ms / 1000.0)
    continuous_drive = pulse_window_s <= 0.0
    # For continuous drive we track the actual time since each pulse update to
    # integrate theta correctly. For burst mode we use the window length.
    prev_pulse_t = time.time()
    prev_yaw_us_active = yaw_stop
    prev_tilt_us_active = tilt_stop
    # Slew-limited signed Δus. Written after every servo decision.
    prev_yaw_du = 0
    prev_tilt_du = 0
    # Background thread schedules the "park to stop" after a burst so the
    # main capture/detect loop is not blocked by the sleep. Only used in
    # burst mode; harmless if never armed.
    _servo_lock = threading.Lock()
    _servo_next_park_t = [0.0]

    def _servo_parker():
        while not stop[0]:
            time.sleep(0.005)
            t = _servo_next_park_t[0]
            if t and time.time() >= t:
                with _servo_lock:
                    if pca and _servo_next_park_t[0] == t:
                        pca.set_pulse_us(args.yaw_ch, yaw_stop)
                        pca.set_pulse_us(args.tilt_ch, tilt_stop)
                        _servo_next_park_t[0] = 0.0

    if pca and not continuous_drive:
        threading.Thread(target=_servo_parker, daemon=True).start()
    print(f"[init] auto-zero: theta_yaw=0 theta_tilt=0 "
          f"(ensure mechanism is in home pose before starting)")
    print(f"[init] limits: yaw [{theta_yaw_min:+.0f},{theta_yaw_max:+.0f}] deg  "
          f"tilt [{theta_tilt_min:+.0f},{theta_tilt_max:+.0f}] deg")
    print(f"[init] drive mode: "
          f"{'continuous' if continuous_drive else f'burst {args.pulse_window_ms}ms'}")

    try:
        while not stop[0]:
            # Drain stale frames from V4L2 buffer so we always process the
            # newest capture — otherwise fps < camera rate builds backlog.
            for _ in range(max(0, args.flush_frames)):
                cap.grab()
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.01)
                continue
            # Use actual frame geometry -- camera may ignore requested WxH.
            fh, fw = frame.shape[:2]
            cx_img, cy_img = fw // 2, fh // 2
            if not real_dims_logged:
                print(f"[cam] actual frame {fw}x{fh} (requested "
                      f"{args.width}x{args.height})", flush=True)
                real_dims_logged = True
            frames += 1

            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # Detection on a downscaled copy is ~4x faster; scale results back.
            ds = max(0.1, min(1.0, args.det_scale))
            if ds < 0.99:
                det_gray = cv2.resize(gray, None, fx=ds, fy=ds,
                                       interpolation=cv2.INTER_AREA)
            else:
                det_gray = gray
            det_gray = cv2.equalizeHist(det_gray)
            min_face_ds = max(10, int(args.min_face * ds))
            faces = face_cascade.detectMultiScale(
                det_gray, scaleFactor=1.2, minNeighbors=args.min_neighbors,
                minSize=(min_face_ds, min_face_ds),
                flags=cv2.CASCADE_SCALE_IMAGE,
            )
            det_kind = "frontal" if len(faces) else "-"
            # Frontal missed -> try profile (right-facing), then flip for left-facing
            if len(faces) == 0 and profile_cascade is not None:
                prof = profile_cascade.detectMultiScale(
                    det_gray, scaleFactor=1.2, minNeighbors=args.min_neighbors,
                    minSize=(min_face_ds, min_face_ds),
                    flags=cv2.CASCADE_SCALE_IMAGE,
                )
                if len(prof):
                    faces = prof
                    det_kind = "profile-R"
                else:
                    flipped = cv2.flip(det_gray, 1)
                    prof = profile_cascade.detectMultiScale(
                        flipped, scaleFactor=1.2, minNeighbors=args.min_neighbors,
                        minSize=(min_face_ds, min_face_ds),
                        flags=cv2.CASCADE_SCALE_IMAGE,
                    )
                    if len(prof):
                        w_ds = det_gray.shape[1]
                        faces = [(w_ds - x - w, y, w, h) for (x, y, w, h) in prof]
                        det_kind = "profile-L"

            # Scale bboxes back to original frame coordinates.
            if ds < 0.99 and len(faces):
                inv = 1.0 / ds
                faces = [(int(x * inv), int(y * inv),
                          int(w * inv), int(h * inv)) for (x, y, w, h) in faces]

            # Skin-tone gate: reject candidates whose central YCrCb chroma
            # is outside human skin range (kills overexposed white patches
            # that Haar sometimes mistakes for a face).
            rejected = 0
            if args.skin_check and len(faces):
                ycrcb = cv2.cvtColor(frame, cv2.COLOR_BGR2YCrCb)
                kept = []
                for (x, y, w, h) in faces:
                    # sample the central 60% of the bbox to avoid hair/background
                    mx = x + int(w * 0.2); my = y + int(h * 0.2)
                    mw = int(w * 0.6); mh = int(h * 0.6)
                    x0 = max(0, mx); y0 = max(0, my)
                    x1 = min(fw, mx + mw); y1 = min(fh, my + mh)
                    if x1 <= x0 or y1 <= y0:
                        continue
                    patch = ycrcb[y0:y1, x0:x1]
                    cr = int(cv2.medianBlur(patch[:, :, 1], 5).mean())
                    cb = int(cv2.medianBlur(patch[:, :, 2], 5).mean())
                    if (args.skin_cr_min <= cr <= args.skin_cr_max and
                            args.skin_cb_min <= cb <= args.skin_cb_max):
                        kept.append((x, y, w, h))
                    else:
                        rejected += 1
                faces = kept
                if not faces:
                    det_kind = f"reject-skin({rejected})"

            face = None
            if len(faces) > 0:
                face = max(faces, key=lambda r: r[2] * r[3])

            err_x = err_y = 0
            yaw_limited = tilt_limited = False
            if face is not None:
                lost = 0
                x, y, w, h = face
                fx = x + w // 2
                fy = y + h // 2
                err_x = fx - cx_img
                err_y = fy - cy_img

                yaw_us = compute_pulse(err_x, yaw_stop, args.yaw_gain,
                                       args.yaw_invert, args.yaw_deadband_px,
                                       args.yaw_min_speed_us, args.max_speed_us)
                tilt_us = compute_pulse(err_y, tilt_stop, args.tilt_gain,
                                        args.tilt_invert, args.tilt_deadband_px,
                                        args.tilt_min_speed_us, args.max_speed_us)

                yaw_us, yaw_limited = apply_angle_limit(
                    yaw_us, yaw_stop, theta_yaw, theta_yaw_min, theta_yaw_max,
                    args.brake_band_deg)
                tilt_us, tilt_limited = apply_angle_limit(
                    tilt_us, tilt_stop, theta_tilt, theta_tilt_min, theta_tilt_max,
                    args.brake_band_deg)

                # Slew-limit Δus per frame to bound acceleration -> smoother
                # motion, no visible "急停" jerks. Applied AFTER limits so
                # brake band + slew combine into a gentle deceleration.
                if args.du_slew_us > 0:
                    target_yaw_du = yaw_us - yaw_stop
                    max_step = args.du_slew_us
                    target_yaw_du = clamp(target_yaw_du,
                                           prev_yaw_du - max_step,
                                           prev_yaw_du + max_step)
                    yaw_us = yaw_stop + target_yaw_du
                    target_tilt_du = tilt_us - tilt_stop
                    target_tilt_du = clamp(target_tilt_du,
                                            prev_tilt_du - max_step,
                                            prev_tilt_du + max_step)
                    tilt_us = tilt_stop + target_tilt_du
                    prev_yaw_du = target_yaw_du
                    prev_tilt_du = target_tilt_du
                else:
                    prev_yaw_du = yaw_us - yaw_stop
                    prev_tilt_du = tilt_us - tilt_stop

                # Integrate theta from the pulse that was active over the
                # interval since the last update, THEN apply the new pulse.
                # theta_scale amplifies the estimate to compensate for servo
                # momentum + model under-prediction.
                now_t = time.time()
                dt_active = now_t - prev_pulse_t
                theta_yaw += omega_deg_s(
                    prev_yaw_us_active - yaw_stop, args.yaw_speed_slope,
                    args.yaw_speed_intercept, args.yaw_min_speed_us
                ) * dt_active * args.theta_scale
                theta_tilt += omega_deg_s(
                    prev_tilt_us_active - tilt_stop, args.tilt_speed_slope,
                    args.tilt_speed_intercept, args.tilt_min_speed_us
                ) * dt_active * args.theta_scale

                # Continuous drive (recommended): pulse rides until next update.
                # Burst drive (legacy, low fps): background parker sends stop
                # after pulse_window_s.
                if pca:
                    if continuous_drive:
                        pca.set_pulse_us(args.yaw_ch, yaw_us)
                        pca.set_pulse_us(args.tilt_ch, tilt_us)
                    else:
                        with _servo_lock:
                            pca.set_pulse_us(args.yaw_ch, yaw_us)
                            pca.set_pulse_us(args.tilt_ch, tilt_us)
                            if yaw_us != yaw_stop or tilt_us != tilt_stop:
                                _servo_next_park_t[0] = now_t + pulse_window_s
                            else:
                                _servo_next_park_t[0] = 0.0
                prev_pulse_t = now_t
                prev_yaw_us_active = yaw_us
                prev_tilt_us_active = tilt_us

                if not args.kiosk:
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
                    prev_yaw_us_active = yaw_stop
                    prev_tilt_us_active = tilt_stop
                    prev_pulse_t = time.time()
                    prev_yaw_du = 0
                    prev_tilt_du = 0

            cv2.drawMarker(frame, (cx_img, cy_img), (0, 0, 255),
                           cv2.MARKER_CROSS, 20, 2)
            lim_str = ""
            if yaw_limited: lim_str += "YL "
            if tilt_limited: lim_str += "TL "
            hud = (f"yaw={yaw_us}us[{theta_yaw:+.0f}]  "
                   f"tilt={tilt_us}us[{theta_tilt:+.0f}]  "
                   f"err=({err_x:+d},{err_y:+d})px  "
                   f"faces={len(faces)}[{det_kind}]  {lim_str}lost={lost}  "
                   f"fps={running_fps:.1f}")
            if not args.kiosk:
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
                        f"err_x={err_x:+d}  err_y={err_y:+d}  "
                        f"(deadband yaw=+/-{args.yaw_deadband_px} "
                        f"tilt=+/-{args.tilt_deadband_px})\n"
                        f"yaw_us={yaw_us}  tilt_us={tilt_us}  "
                        f"stop={yaw_stop}/{tilt_stop}\n"
                        f"theta_yaw={theta_yaw:+.1f}deg "
                        f"[{theta_yaw_min:+.0f},{theta_yaw_max:+.0f}]  "
                        f"theta_tilt={theta_tilt:+.1f}deg "
                        f"[{theta_tilt_min:+.0f},{theta_tilt_max:+.0f}]\n"
                        f"limit: yaw={'HIT' if yaw_limited else 'ok'}  "
                        f"tilt={'HIT' if tilt_limited else 'ok'}\n"
                        f"gain yaw={args.yaw_gain} tilt={args.tilt_gain}  "
                        f"min_us yaw={args.yaw_min_speed_us} tilt={args.tilt_min_speed_us}  "
                        f"max_us={args.max_speed_us}\n"
                        f"invert yaw={args.yaw_invert} tilt={args.tilt_invert}  "
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
