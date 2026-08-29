"""Makeup mirror all-in-one detector.

One class produces a single JSON payload consumed by the unified HUD:

  face          — bbox (or None)
  regions       — per-zone evenness score (forehead / cheeks / chin)
  symmetry      — LAB color delta between L/R cheeks
  blemishes     — pimple / red-spot candidates: count + bboxes
  tzone         — oily / shine ratio on forehead + nose bridge
  dark_circles  — under-eye vs cheek L delta (per side + overall)
  lighting      — overall exposure + left/right imbalance
  timeline      — rolling series of overall unevenness with baseline drift
  overall       — top-line score + verdict

All measurements are heuristics on webcam frames: OpenCV + numpy only, no ML.
Precision is deliberately modest; the goal is to draw the eye to obvious
problems, not to be a dermatologist.

Two things are stubbed pending face landmarks (MediaPipe / dlib): geometric
symmetry (brow/eye/mouth) and lipstick/eyeliner bleed detection. The rest
lives on proportional bboxes inside the Haar face rect — crude but robust.
"""

from __future__ import annotations

import collections
import os
import time

import cv2
import numpy as np

WORK_WIDTH = 640
TIMELINE_SAMPLE_INTERVAL = 2.0   # store one timeline sample every N seconds
TIMELINE_KEEP_SEC = 600          # ...for this long
TIMELINE_BASELINE_AFTER_SEC = 4  # first stable sample becomes the baseline

# ==== SKIN GATE (YCrCb) — reused everywhere skin pixels matter =============
SKIN_CR_MIN, SKIN_CR_MAX = 133, 173
SKIN_CB_MIN, SKIN_CB_MAX = 77, 127

# ==== EVENNESS: 4 face zones =============================================
EVEN_REGIONS = {
    "forehead": (0.20, 0.08, 0.80, 0.28),
    "l_cheek":  (0.12, 0.45, 0.38, 0.72),
    "r_cheek":  (0.62, 0.45, 0.88, 0.72),
    "chin":     (0.32, 0.78, 0.68, 0.95),
}
W_STD_L, W_STD_A, W_PATCH = 0.35, 0.35, 0.30
PATCH_PX = 12
MIN_SKIN_PIXELS = 200
NORM_STD_L, NORM_STD_A, NORM_STD_PATCH = 12.0, 4.0, 8.0
SCORE_OK, SCORE_WARN = 25, 45

# ==== COLOR SYMMETRY (L/R cheek) ==========================================
SYM_L_WARN, SYM_L_ALERT = 6.0, 12.0
SYM_A_WARN, SYM_A_ALERT = 3.0, 6.0

# ==== BLEMISHES ===========================================================
# Red-peaks (LAB `a` channel top-hat) inside skin mask, within the face bbox.
BLEMISH_TOPHAT_KERNEL = 15
BLEMISH_A_MIN = 6                # top-hat threshold on `a`
BLEMISH_AREA_MIN, BLEMISH_AREA_MAX = 4, 250
BLEMISH_MIN_CIRC = 0.35
BLEMISH_WARN, BLEMISH_ALERT = 3, 8

# ==== T-ZONE SHINE ========================================================
# Very bright, very desaturated pixels within skin mask on the T-zone.
TZONE_REGIONS = {
    "forehead_full": (0.18, 0.05, 0.82, 0.32),
    "nose_bridge":   (0.42, 0.30, 0.58, 0.60),
}
SHINE_V_MIN = 225
SHINE_S_MAX = 45
SHINE_RATIO_WARN, SHINE_RATIO_ALERT = 0.04, 0.10

# ==== DARK CIRCLES ========================================================
# Undereye strip vs a mid-cheek reference strip on the same side.
UNDEREYE_REGIONS = {
    "l_under": (0.20, 0.30, 0.42, 0.44),
    "r_under": (0.58, 0.30, 0.80, 0.44),
}
CHEEK_REF_REGIONS = {
    "l_ref":   (0.15, 0.55, 0.35, 0.65),
    "r_ref":   (0.65, 0.55, 0.85, 0.65),
}
DARK_L_WARN, DARK_L_ALERT = 6.0, 12.0

# ==== LIGHTING & POSE =====================================================
FACE_L_LOW, FACE_L_HIGH = 90, 205       # skin-mean brightness bounds
LIGHT_BAL_WARN, LIGHT_BAL_ALERT = 12.0, 25.0
POSE_OFFSET_WARN = 0.15                 # face-center horizontal offset ratio
POSE_SIZE_WARN = 0.18                   # face too small in frame


# --- helpers --------------------------------------------------------------

def _clip_box(x, y, w, h, W, H):
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(W, x + w), min(H, y + h)
    return x0, y0, x1, y1


def _verdict3(score: float, warn: float, alert: float) -> str:
    if score >= alert:
        return "alert"
    if score >= warn:
        return "warn"
    return "ok"


class _Timeline:
    """Rolling (timestamp, overall_score) samples with a fixed baseline.

    Baseline is set to the first stable sample after ~4s of a detected face,
    so the drift number reflects "how far you've drifted since you sat down",
    which is what a mirror should be reporting.
    """

    def __init__(self):
        self._buf: collections.deque[tuple[float, float]] = collections.deque()
        self._last_push = 0.0
        self._baseline: float | None = None
        self._baseline_after: float | None = None

    def push(self, now: float, overall: float | None) -> None:
        if overall is None:
            return
        if now - self._last_push < TIMELINE_SAMPLE_INTERVAL:
            return
        self._last_push = now
        self._buf.append((now, overall))
        cutoff = now - TIMELINE_KEEP_SEC
        while self._buf and self._buf[0][0] < cutoff:
            self._buf.popleft()
        if self._baseline is None:
            if self._baseline_after is None:
                self._baseline_after = now + TIMELINE_BASELINE_AFTER_SEC
            elif now >= self._baseline_after:
                # Median of what we have so far — robust to one weird frame.
                vals = [v for _, v in self._buf]
                self._baseline = float(np.median(vals)) if vals else overall

    def snapshot(self, now: float) -> dict:
        # Downsample to at most ~120 points for the UI sparkline.
        pts = list(self._buf)
        if len(pts) > 120:
            step = len(pts) // 120
            pts = pts[::step]
        series = [{"t": round(now - t, 1), "s": round(s, 1)} for t, s in pts]
        drift = None
        if self._baseline is not None and self._buf:
            drift = round(self._buf[-1][1] - self._baseline, 1)
        verdict = "ok"
        if drift is not None:
            if drift >= 15:
                verdict = "alert"
            elif drift >= 7:
                verdict = "warn"
        return {
            "baseline": (None if self._baseline is None
                         else round(self._baseline, 1)),
            "drift": drift,
            "verdict": verdict,
            "points": series,
        }


class MakeupDetector:
    def __init__(self) -> None:
        cascade_path = os.path.join(cv2.data.haarcascades,
                                    "haarcascade_frontalface_default.xml")
        self._face_cascade = cv2.CascadeClassifier(cascade_path)
        if self._face_cascade.empty():
            raise RuntimeError(f"Could not load Haar cascade from {cascade_path}")
        self._blemish_k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (BLEMISH_TOPHAT_KERNEL, BLEMISH_TOPHAT_KERNEL))
        self._timeline = _Timeline()

    # ---------- face ----------
    def _find_face(self, gray: np.ndarray):
        faces = self._face_cascade.detectMultiScale(
            gray, scaleFactor=1.2, minNeighbors=5,
            minSize=(120, 120), flags=cv2.CASCADE_SCALE_IMAGE)
        if len(faces) == 0:
            return None
        faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
        return tuple(int(v) for v in faces[0])

    # ---------- evenness ----------
    def _score_evenness(self, lab_roi, skin_roi):
        skin_px = int(np.count_nonzero(skin_roi))
        if skin_px < MIN_SKIN_PIXELS:
            return None
        L, a, _b = cv2.split(lab_roi)
        m = skin_roi > 0
        std_L = float(L[m].std()); std_a = float(a[m].std())
        mean_L = float(L[m].mean()); mean_a = float(a[m].mean())
        h, w = L.shape
        gy, gx = max(1, h // PATCH_PX), max(1, w // PATCH_PX)
        patch_means = []
        for iy in range(gy):
            for ix in range(gx):
                y0, x0 = iy * PATCH_PX, ix * PATCH_PX
                y1, x1 = min(y0 + PATCH_PX, h), min(x0 + PATCH_PX, w)
                pm = m[y0:y1, x0:x1]
                if np.count_nonzero(pm) < 8:
                    continue
                patch_means.append(float(L[y0:y1, x0:x1][pm].mean()))
        std_patch = float(np.std(patch_means)) if len(patch_means) >= 4 else 0.0
        raw = (W_STD_L * std_L / NORM_STD_L
               + W_STD_A * std_a / NORM_STD_A
               + W_PATCH * std_patch / NORM_STD_PATCH)
        score = float(np.clip(raw * 100.0, 0.0, 100.0))
        return {"score": round(score, 1), "mean_L": round(mean_L, 1),
                "mean_a": round(mean_a, 1), "skin_px": skin_px}

    # ---------- blemishes ----------
    def _detect_blemishes(self, face_bgr, face_lab, skin_full, fx, fy, inv):
        _L, a, _b = cv2.split(face_lab)
        # White top-hat on `a` isolates local redness peaks.
        tophat = cv2.morphologyEx(a, cv2.MORPH_TOPHAT, self._blemish_k)
        cand = ((tophat >= BLEMISH_A_MIN) & (skin_full > 0)).astype(np.uint8) * 255
        n, labels, stats, cents = cv2.connectedComponentsWithStats(cand, 8)
        boxes = []
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < BLEMISH_AREA_MIN or area > BLEMISH_AREA_MAX:
                continue
            x = int(stats[i, cv2.CC_STAT_LEFT]); y = int(stats[i, cv2.CC_STAT_TOP])
            bw = int(stats[i, cv2.CC_STAT_WIDTH]); bh = int(stats[i, cv2.CC_STAT_HEIGHT])
            comp = (labels[y:y + bh, x:x + bw] == i).astype(np.uint8)
            contours, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                continue
            cnt = contours[0]
            per = cv2.arcLength(cnt, True)
            if per <= 0:
                continue
            circ = 4.0 * np.pi * area / (per * per)
            if circ < BLEMISH_MIN_CIRC:
                continue
            boxes.append({
                "x": int((fx + x) * inv), "y": int((fy + y) * inv),
                "w": max(1, int(bw * inv)), "h": max(1, int(bh * inv)),
            })
        verdict = ("ok" if len(boxes) < BLEMISH_WARN
                   else "warn" if len(boxes) < BLEMISH_ALERT
                   else "alert")
        return {"count": len(boxes), "boxes": boxes, "verdict": verdict}

    # ---------- T-zone shine ----------
    def _detect_shine(self, face_hsv, skin_full, fw, fh, fx, fy, inv):
        H, S, V = cv2.split(face_hsv)
        shiny = ((V >= SHINE_V_MIN) & (S <= SHINE_S_MAX)
                 & (skin_full > 0)).astype(np.uint8)
        parts = {}
        rects_out = {}
        total_shine = 0
        total_skin = 0
        for name, (rx0, ry0, rx1, ry1) in TZONE_REGIONS.items():
            x0 = int(rx0 * fw); y0 = int(ry0 * fh)
            x1 = int(rx1 * fw); y1 = int(ry1 * fh)
            sub_skin = skin_full[y0:y1, x0:x1]
            sub_shine = shiny[y0:y1, x0:x1]
            skin_n = int(np.count_nonzero(sub_skin))
            shine_n = int(np.count_nonzero(sub_shine))
            ratio = (shine_n / skin_n) if skin_n >= 40 else None
            parts[name] = None if ratio is None else round(ratio, 3)
            rects_out[name] = {
                "x": int((fx + x0) * inv), "y": int((fy + y0) * inv),
                "w": int((x1 - x0) * inv), "h": int((y1 - y0) * inv),
            }
            if ratio is not None:
                total_shine += shine_n
                total_skin += skin_n
        if total_skin < 200:
            return {"ratio": None, "parts": parts, "rects": rects_out,
                    "verdict": "unknown"}
        ratio = total_shine / total_skin
        verdict = _verdict3(ratio, SHINE_RATIO_WARN, SHINE_RATIO_ALERT)
        return {"ratio": round(ratio, 3), "parts": parts, "rects": rects_out,
                "verdict": verdict}

    # ---------- dark circles ----------
    def _score_dark_circles(self, face_lab, skin_full, fw, fh, fx, fy, inv):
        L, _a, _b = cv2.split(face_lab)

        def mean_L(rect):
            rx0, ry0, rx1, ry1 = rect
            x0 = int(rx0 * fw); y0 = int(ry0 * fh)
            x1 = int(rx1 * fw); y1 = int(ry1 * fh)
            sub_L = L[y0:y1, x0:x1]
            sub_skin = skin_full[y0:y1, x0:x1]
            m = sub_skin > 0
            if int(np.count_nonzero(m)) < 60:
                return None, {"x": int((fx + x0) * inv), "y": int((fy + y0) * inv),
                              "w": int((x1 - x0) * inv), "h": int((y1 - y0) * inv)}
            return float(sub_L[m].mean()), {"x": int((fx + x0) * inv),
                                             "y": int((fy + y0) * inv),
                                             "w": int((x1 - x0) * inv),
                                             "h": int((y1 - y0) * inv)}

        sides = {}
        rects = {}
        for side, under_key, ref_key in (("left", "l_under", "l_ref"),
                                         ("right", "r_under", "r_ref")):
            uL, u_rect = mean_L(UNDEREYE_REGIONS[under_key])
            rL, r_rect = mean_L(CHEEK_REF_REGIONS[ref_key])
            rects[under_key] = u_rect
            rects[ref_key] = r_rect
            if uL is None or rL is None:
                sides[side] = None
            else:
                delta = rL - uL   # positive = undereye darker than cheek
                sides[side] = round(delta, 2)
        valid = [v for v in sides.values() if v is not None]
        if not valid:
            return {"sides": sides, "rects": rects, "score": None,
                    "verdict": "unknown"}
        worst = max(valid)
        verdict = _verdict3(worst, DARK_L_WARN, DARK_L_ALERT)
        return {"sides": sides, "rects": rects, "score": round(worst, 2),
                "verdict": verdict}

    # ---------- lighting & pose ----------
    def _score_lighting(self, face_lab, skin_full, fx, fy, fw, fh, frame_w, frame_h):
        L, _a, _b = cv2.split(face_lab)
        m = skin_full > 0
        if int(np.count_nonzero(m)) < 500:
            return {"verdict": "unknown", "notes": ["face too small / low skin coverage"]}
        mean_L = float(L[m].mean())
        # Left / right halves of the face bbox.
        half = fw // 2
        lm = m[:, :half]
        rm = m[:, half:]
        L_left = float(L[:, :half][lm].mean()) if np.any(lm) else mean_L
        L_right = float(L[:, half:][rm].mean()) if np.any(rm) else mean_L
        bal = abs(L_left - L_right)

        # Pose from Haar bbox alone: center offset + size relative to frame.
        cx = fx + fw / 2.0
        offset_ratio = (cx - frame_w / 2.0) / (frame_w / 2.0)
        size_ratio = fw / float(frame_w)

        notes = []
        verdict = "ok"
        if mean_L < FACE_L_LOW:
            verdict = "warn"; notes.append("光线偏暗，靠近光源")
        elif mean_L > FACE_L_HIGH:
            verdict = "warn"; notes.append("过曝，避开强直射光")
        if bal >= LIGHT_BAL_ALERT:
            verdict = "alert"; notes.append("光线偏一侧，补个侧光")
        elif bal >= LIGHT_BAL_WARN and verdict != "alert":
            verdict = "warn"; notes.append("左右光不均")
        if abs(offset_ratio) > POSE_OFFSET_WARN:
            notes.append("脸偏离画面中心，端正一下")
        if size_ratio < POSE_SIZE_WARN:
            notes.append("靠近一点看得更准")

        return {
            "mean_L": round(mean_L, 1),
            "L_left": round(L_left, 1),
            "L_right": round(L_right, 1),
            "balance": round(bal, 1),
            "offset": round(offset_ratio, 3),
            "size": round(size_ratio, 3),
            "verdict": verdict,
            "notes": notes,
        }

    # ---------- MAIN ----------
    def detect(self, frame_bgr: np.ndarray) -> dict:
        now = time.time()
        h0, w0 = frame_bgr.shape[:2]
        scale = WORK_WIDTH / float(w0)
        if scale < 1.0:
            work = cv2.resize(frame_bgr, (WORK_WIDTH, int(h0 * scale)),
                              interpolation=cv2.INTER_AREA)
        else:
            work = frame_bgr.copy()
            scale = 1.0
        inv = 1.0 / scale
        Wh, Ww = work.shape[:2]

        result = {
            "mode": "makeup",
            "face": None,
            "regions": {},
            "symmetry": None,
            "blemishes": {"count": 0, "boxes": [], "verdict": "unknown"},
            "tzone": {"ratio": None, "verdict": "unknown", "rects": {}},
            "dark_circles": {"score": None, "verdict": "unknown", "rects": {},
                              "sides": {"left": None, "right": None}},
            "lighting": {"verdict": "unknown", "notes": []},
            "geom_symmetry": None,   # needs landmarks
            "bleed": None,           # needs landmarks
            "timeline": self._timeline.snapshot(now),
            "overall": None,
            # HUD parity with old dandruff page:
            "count": 0, "detections": [], "hair_ratio": 0.0,
        }

        gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
        gray = cv2.equalizeHist(gray)
        face = self._find_face(gray)
        if face is None:
            self._timeline.push(now, None)
            return result

        fx, fy, fw, fh = face
        result["face"] = {"x": int(fx * inv), "y": int(fy * inv),
                          "w": int(fw * inv), "h": int(fh * inv)}

        face_bgr = work[fy:fy + fh, fx:fx + fw]
        if face_bgr.size == 0:
            return result

        face_lab = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2LAB)
        face_ycc = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2YCrCb)
        _Y, Cr, Cb = cv2.split(face_ycc)
        skin_full = (((Cr >= SKIN_CR_MIN) & (Cr <= SKIN_CR_MAX)
                      & (Cb >= SKIN_CB_MIN) & (Cb <= SKIN_CB_MAX))
                     .astype(np.uint8) * 255)
        face_hsv = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2HSV)

        # --- evenness per zone ------------------------------------------
        even_scores = []
        for name, (rx0, ry0, rx1, ry1) in EVEN_REGIONS.items():
            x0 = int(rx0 * fw); y0 = int(ry0 * fh)
            x1 = int(rx1 * fw); y1 = int(ry1 * fh)
            lab_roi = face_lab[y0:y1, x0:x1]
            skin_roi = skin_full[y0:y1, x0:x1]
            rect = {"x": int((fx + x0) * inv), "y": int((fy + y0) * inv),
                    "w": int((x1 - x0) * inv), "h": int((y1 - y0) * inv)}
            if lab_roi.size == 0:
                continue
            stats = self._score_evenness(lab_roi, skin_roi)
            if stats is None:
                result["regions"][name] = {"rect": rect, "score": None,
                                            "verdict": "unknown"}
                continue
            score = stats["score"]
            verdict = _verdict3(score, SCORE_OK, SCORE_WARN)
            result["regions"][name] = {"rect": rect, "verdict": verdict, **stats}
            even_scores.append(score)

        # --- color symmetry --------------------------------------------
        l = result["regions"].get("l_cheek")
        r = result["regions"].get("r_cheek")
        if l and r and l.get("score") is not None and r.get("score") is not None:
            dL = abs(l["mean_L"] - r["mean_L"])
            da = abs(l["mean_a"] - r["mean_a"])
            sym_score = float(np.clip(
                50.0 * (dL / SYM_L_ALERT) + 50.0 * (da / SYM_A_ALERT), 0.0, 100.0))
            sym_v = "ok"
            if dL > SYM_L_ALERT or da > SYM_A_ALERT:
                sym_v = "alert"
            elif dL > SYM_L_WARN or da > SYM_A_WARN:
                sym_v = "warn"
            result["symmetry"] = {"dL": round(dL, 2), "da": round(da, 2),
                                   "score": round(sym_score, 1), "verdict": sym_v}

        # --- blemishes -------------------------------------------------
        result["blemishes"] = self._detect_blemishes(
            face_bgr, face_lab, skin_full, fx, fy, inv)

        # --- T-zone shine ---------------------------------------------
        result["tzone"] = self._detect_shine(face_hsv, skin_full, fw, fh, fx, fy, inv)

        # --- dark circles ----------------------------------------------
        result["dark_circles"] = self._score_dark_circles(
            face_lab, skin_full, fw, fh, fx, fy, inv)

        # --- lighting & pose ------------------------------------------
        result["lighting"] = self._score_lighting(
            face_lab, skin_full, fx, fy, fw, fh, Ww, Wh)

        # --- overall aggregate ----------------------------------------
        parts = []
        if even_scores:
            parts.append(("evenness", float(np.mean(even_scores)), 1.0))
        if result["symmetry"] is not None:
            parts.append(("symmetry", result["symmetry"]["score"], 0.8))
        # Blemishes: normalize count into 0..100.
        b_score = min(100.0, result["blemishes"]["count"] * 10.0)
        parts.append(("blemishes", b_score, 0.7))
        # Shine: ratio 0..0.2 → 0..100.
        if result["tzone"]["ratio"] is not None:
            parts.append(("tzone", min(100.0, result["tzone"]["ratio"] * 500.0), 0.5))
        # Dark circles: worst delta L 0..15 → 0..100.
        if result["dark_circles"]["score"] is not None:
            parts.append(("dark", min(100.0, max(0.0, result["dark_circles"]["score"] * 8.0)), 0.5))

        overall_score = None
        if parts:
            wsum = sum(w for _, _, w in parts)
            overall_score = sum(s * w for _, s, w in parts) / wsum
            verdict = _verdict3(overall_score, SCORE_OK, SCORE_WARN)
            result["overall"] = {"score": round(overall_score, 1),
                                  "verdict": verdict}

        self._timeline.push(now, overall_score)
        result["timeline"] = self._timeline.snapshot(now)
        return result

    # ==================== OVERLAY ==========================================
    _COLORS = {
        "ok":     (80, 220, 130),
        "warn":   (63, 210, 255),
        "alert":  (94, 77, 255),
        "unknown": (140, 140, 140),
    }

    @classmethod
    def draw(cls, frame_bgr: np.ndarray, result: dict) -> np.ndarray:
        out = frame_bgr
        face = result.get("face")
        if face is None:
            cv2.rectangle(out, (10, 10), (340, 70), (0, 0, 0), -1)
            cv2.putText(out, "no face", (20, 52), cv2.FONT_HERSHEY_SIMPLEX,
                        1.1, (180, 180, 180), 2, cv2.LINE_AA)
            return out

        cv2.rectangle(out, (face["x"], face["y"]),
                      (face["x"] + face["w"], face["y"] + face["h"]),
                      (200, 200, 200), 1)

        # Evenness regions.
        for name, reg in result.get("regions", {}).items():
            rect = reg["rect"]
            color = cls._COLORS.get(reg.get("verdict", "unknown"),
                                    cls._COLORS["unknown"])
            cv2.rectangle(out, (rect["x"], rect["y"]),
                          (rect["x"] + rect["w"], rect["y"] + rect["h"]),
                          color, 2)
            score = reg.get("score")
            tag = f"{name}: " + ("--" if score is None else f"{score:.0f}")
            cv2.putText(out, tag, (rect["x"], max(0, rect["y"] - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1, cv2.LINE_AA)

        # Blemishes.
        for b in result.get("blemishes", {}).get("boxes", []):
            cx = b["x"] + b["w"] // 2
            cy = b["y"] + b["h"] // 2
            r = max(6, int(1.6 * max(b["w"], b["h"])))
            cv2.circle(out, (cx, cy), r, (77, 77, 255), 2)

        # Dark-circle strips (dashed feel via 1-px rectangles).
        for name, rect in result.get("dark_circles", {}).get("rects", {}).items():
            color = (200, 200, 90) if name.endswith("_under") else (110, 110, 110)
            cv2.rectangle(out, (rect["x"], rect["y"]),
                          (rect["x"] + rect["w"], rect["y"] + rect["h"]),
                          color, 1)

        # T-zone.
        for name, rect in result.get("tzone", {}).get("rects", {}).items():
            v = result["tzone"].get("verdict", "unknown")
            color = cls._COLORS.get(v, cls._COLORS["unknown"])
            cv2.rectangle(out, (rect["x"], rect["y"]),
                          (rect["x"] + rect["w"], rect["y"] + rect["h"]),
                          color, 1)

        # Header: overall + lighting hint.
        overall = result.get("overall")
        cv2.rectangle(out, (10, 10), (420, 78), (0, 0, 0), -1)
        if overall is None:
            cv2.putText(out, "scoring...", (20, 55), cv2.FONT_HERSHEY_SIMPLEX,
                        1.1, (200, 200, 200), 2, cv2.LINE_AA)
        else:
            color = cls._COLORS[overall["verdict"]]
            cv2.putText(out, f"score {overall['score']:.0f}",
                        (20, 55), cv2.FONT_HERSHEY_SIMPLEX, 1.1, color, 2,
                        cv2.LINE_AA)
        return out
