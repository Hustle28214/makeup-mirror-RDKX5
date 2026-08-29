"""V4L2 camera capture wrapper.

Runs a grab thread so the HTTP handler always serves the freshest frame instead
of a stale one from OpenCV's internal buffer (which stalls MJPEG under load).
"""

from __future__ import annotations

import sys
import threading
import time

import cv2


def _parse_device(device):
    """Accept int, numeric string (Windows index), or V4L2 path."""
    if isinstance(device, int):
        return device
    if isinstance(device, str) and device.isdigit():
        return int(device)
    return device


class Camera:
    def __init__(self, device="/dev/video0",
                 width: int = 1920, height: int = 1080, fps: int = 30) -> None:
        self.device = _parse_device(device)
        self.width = width
        self.height = height
        self.fps = fps
        self._cap: cv2.VideoCapture | None = None
        self._frame = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _open(self) -> cv2.VideoCapture:
        # Pick the backend per OS. Windows: MSMF first (fast, exposes MJPG),
        # then DSHOW fallback. Linux/RDK: V4L2.
        if sys.platform.startswith("win"):
            backends = [cv2.CAP_MSMF, cv2.CAP_DSHOW, cv2.CAP_ANY]
        else:
            backends = [cv2.CAP_V4L2, cv2.CAP_ANY]
        for be in backends:
            cap = cv2.VideoCapture(self.device, be)
            if cap.isOpened():
                return cap
            cap.release()
        raise RuntimeError(f"Cannot open camera {self.device!r}")

    def start(self) -> None:
        cap = self._open()

        # MJPG lets most USB webcams actually hit 1080p30. On Windows MSMF this
        # is required for anything above 640x480@30 on most UVC cams.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self._cap = cap
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        assert self._cap is not None
        fail_streak = 0
        while not self._stop.is_set():
            try:
                ok, frame = self._cap.read()
            except Exception:
                ok, frame = False, None
            if not ok or frame is None:
                fail_streak += 1
                # ~1s of consecutive failures → the device likely disconnected
                # or MSMF wedged. Reopen it.
                if fail_streak >= 100:
                    try:
                        self._cap.release()
                    except Exception:
                        pass
                    try:
                        self._cap = self._open()
                        self._cap.set(cv2.CAP_PROP_FOURCC,
                                      cv2.VideoWriter_fourcc(*"MJPG"))
                        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
                        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
                        self._cap.set(cv2.CAP_PROP_FPS, self.fps)
                        self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                        print("[camera] reopened after failure streak")
                    except Exception as e:
                        print(f"[camera] reopen failed: {e}")
                    fail_streak = 0
                time.sleep(0.01)
                continue
            fail_streak = 0
            with self._lock:
                self._frame = frame

    def read(self):
        with self._lock:
            return None if self._frame is None else self._frame.copy()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._cap is not None:
            self._cap.release()
