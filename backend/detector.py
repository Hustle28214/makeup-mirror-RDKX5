"""Dandruff detector for dark hair — v2, multi-veto pipeline.

Failure modes of the naive top-hat approach and how this version handles them:

  1. Specular highlights along hair strands are elongated. We (a) subtract the
     union of directional line-openings from the candidate mask, and (b) filter
     each surviving connected component by minAreaRect aspect ratio.

  2. Bright pixels *outside* the head (background clutter, skin, clothing) —
     we build the hair mask, keep only its largest connected component, and
     then ERODE it so the ROI is the strict interior of the hair region.

  3. Bright hair partings when the background lights the scalp through the
     gap — partings are long linear structures too, so the line-opening step
     removes them. As a second belt, we require each candidate to be
     surrounded by hair pixels (in the ORIGINAL, un-eroded mask) within a
     small ring — a parting has hair on one side and skin/gap on the other,
     so this ratio drops below threshold.

  4. Local contrast belt: the flake's mean V must exceed the surrounding
     ring's mean V by MIN_CONTRAST — this kills soft gradients and pale
     scalp patches.
"""

from __future__ import annotations

import cv2
import numpy as np

WORK_WIDTH = 960

# ---- Hair mask ----
HAIR_V_MAX = 95        # dark hair upper bound in V
HAIR_S_MAX = 95        # keep desaturated (avoids warm skin)
HAIR_MIN_COMPONENT_RATIO = 0.02  # discard hair CCs smaller than 2% of frame

# ---- Erosion of hair mask into strict ROI ----
HAIR_ERODE_PX = 15     # px @ WORK_WIDTH — pushes ROI away from hair edges

# ---- Flake candidate thresholds ----
TOPHAT_KERNEL = 15
FLAKE_V_MIN = 155
FLAKE_TOPHAT_MIN = 30

# ---- Line-structure suppression (rejects strand highlights + partings) ----
LINE_KERNEL_LEN = 11
LINE_ANGLES_DEG = (0, 30, 60, 90, 120, 150)

# ---- Geometry filter ----
MIN_AREA = 3
MAX_AREA = 200
MIN_CIRCULARITY = 0.55
MAX_ASPECT = 2.4       # minAreaRect long/short — rejects elongated blobs

# ---- Local belts around each candidate ----
RING_INNER = 3         # px — inner ring radius (skip the flake itself)
RING_OUTER = 10        # px — outer ring radius
MIN_HAIR_RING_RATIO = 0.55   # ≥55% of the ring must be hair
MIN_CONTRAST = 35      # blob mean V − ring mean V (0..255)


def _line_kernel(length: int, angle_deg: float) -> np.ndarray:
    """A rotated line-shaped structuring element."""
    k = np.zeros((length, length), dtype=np.uint8)
    cv2.line(k, (0, length // 2), (length - 1, length // 2), 1, 1)
    M = cv2.getRotationMatrix2D((length / 2, length / 2), angle_deg, 1.0)
    k = cv2.warpAffine(k, M, (length, length), flags=cv2.INTER_NEAREST)
    return (k > 0).astype(np.uint8)


class DandruffDetector:
    def __init__(self) -> None:
        self._tophat_k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (TOPHAT_KERNEL, TOPHAT_KERNEL)
        )
        self._hair_morph_k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
        self._hair_erode_k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (HAIR_ERODE_PX, HAIR_ERODE_PX)
        )
        self._ring_outer_k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * RING_OUTER + 1, 2 * RING_OUTER + 1)
        )
        self._ring_inner_k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * RING_INNER + 1, 2 * RING_INNER + 1)
        )
        self._line_kernels = [_line_kernel(LINE_KERNEL_LEN, a)
                              for a in LINE_ANGLES_DEG]

    # ---------- hair mask ----------
    def _hair_mask(self, hsv: np.ndarray) -> np.ndarray:
        H, S, V = cv2.split(hsv)
        mask = ((V <= HAIR_V_MAX) & (S <= HAIR_S_MAX)).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._hair_morph_k)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._hair_morph_k)
        # Keep only the largest connected component — the head region.
        n, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        if n <= 1:
            return mask
        areas = stats[1:, cv2.CC_STAT_AREA]
        biggest = int(np.argmax(areas)) + 1
        biggest_area = int(areas[biggest - 1])
        frame_area = mask.shape[0] * mask.shape[1]
        if biggest_area < HAIR_MIN_COMPONENT_RATIO * frame_area:
            return np.zeros_like(mask)
        return ((labels == biggest).astype(np.uint8)) * 255

    # ---------- line-structure suppression ----------
    def _suppress_lines(self, flake_bin: np.ndarray) -> np.ndarray:
        # Anything preserved by opening with ANY line kernel is line-like.
        line_union = np.zeros_like(flake_bin)
        for k in self._line_kernels:
            opened = cv2.morphologyEx(flake_bin, cv2.MORPH_OPEN, k)
            line_union = cv2.bitwise_or(line_union, opened)
        # Dilate slightly so we also kill the flanks of the line, not just the core.
        line_union = cv2.dilate(line_union, self._ring_inner_k)
        return cv2.bitwise_and(flake_bin, cv2.bitwise_not(line_union))

    # ---------- main ----------
    def detect(self, frame_bgr: np.ndarray) -> dict:
        h0, w0 = frame_bgr.shape[:2]
        scale = WORK_WIDTH / float(w0)
        if scale < 1.0:
            work = cv2.resize(frame_bgr, (WORK_WIDTH, int(h0 * scale)),
                              interpolation=cv2.INTER_AREA)
        else:
            work = frame_bgr.copy()
            scale = 1.0

        hsv = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)
        _, _, V = cv2.split(hsv)

        hair_full = self._hair_mask(hsv)
        hair_strict = cv2.erode(hair_full, self._hair_erode_k)

        # White top-hat on V isolates small local brightness peaks.
        tophat = cv2.morphologyEx(V, cv2.MORPH_TOPHAT, self._tophat_k)
        flake = ((tophat >= FLAKE_TOPHAT_MIN) & (V >= FLAKE_V_MIN)).astype(np.uint8) * 255

        # Restrict to strict hair interior.
        flake = cv2.bitwise_and(flake, hair_strict)

        # Kill line-shaped bright regions (highlights, partings).
        flake = self._suppress_lines(flake)

        n, labels, stats, centroids = cv2.connectedComponentsWithStats(flake, 8)

        # Precompute ring images so we can query per-CC surround stats cheaply.
        # For each candidate we'll pull a local ROI from V and hair_full.
        detections = []
        inv = 1.0 / scale
        for i in range(1, n):
            area = int(stats[i, cv2.CC_STAT_AREA])
            if area < MIN_AREA or area > MAX_AREA:
                continue

            x = int(stats[i, cv2.CC_STAT_LEFT])
            y = int(stats[i, cv2.CC_STAT_TOP])
            bw = int(stats[i, cv2.CC_STAT_WIDTH])
            bh = int(stats[i, cv2.CC_STAT_HEIGHT])

            comp_mask = (labels[y:y + bh, x:x + bw] == i).astype(np.uint8)
            contours, _ = cv2.findContours(comp_mask, cv2.RETR_EXTERNAL,
                                           cv2.CHAIN_APPROX_SIMPLE)
            if not contours:
                continue
            cnt = contours[0]
            perim = cv2.arcLength(cnt, True)
            if perim <= 0:
                continue
            circ = 4.0 * np.pi * area / (perim * perim)
            if circ < MIN_CIRCULARITY:
                continue

            # Aspect ratio via oriented bounding box.
            if len(cnt) >= 3:
                (_, (rw, rh), _) = cv2.minAreaRect(cnt)
                short = min(rw, rh)
                long_ = max(rw, rh)
                if short > 0 and (long_ / short) > MAX_ASPECT:
                    continue

            # Local ring stats.
            cx = int(round(centroids[i, 0]))
            cy = int(round(centroids[i, 1]))
            x0 = max(cx - RING_OUTER, 0)
            y0 = max(cy - RING_OUTER, 0)
            x1 = min(cx + RING_OUTER + 1, V.shape[1])
            y1 = min(cy + RING_OUTER + 1, V.shape[0])
            V_roi = V[y0:y1, x0:x1]
            hair_roi = hair_full[y0:y1, x0:x1]
            if V_roi.size == 0:
                continue
            rh_h, rh_w = V_roi.shape
            yy, xx = np.ogrid[:rh_h, :rh_w]
            lcx = cx - x0
            lcy = cy - y0
            d2 = (xx - lcx) ** 2 + (yy - lcy) ** 2
            ring = (d2 >= RING_INNER ** 2) & (d2 <= RING_OUTER ** 2)
            if not np.any(ring):
                continue

            hair_ring = float(np.count_nonzero(hair_roi[ring] > 0)) / float(np.count_nonzero(ring))
            if hair_ring < MIN_HAIR_RING_RATIO:
                continue

            blob_mean_V = float(V_roi[d2 <= RING_INNER ** 2].mean()) \
                if np.any(d2 <= RING_INNER ** 2) else float(V[cy, cx])
            ring_mean_V = float(V_roi[ring].mean())
            if (blob_mean_V - ring_mean_V) < MIN_CONTRAST:
                continue

            detections.append({
                "x": int(x * inv),
                "y": int(y * inv),
                "w": max(1, int(bw * inv)),
                "h": max(1, int(bh * inv)),
                "area": area,
                "circ": round(float(circ), 3),
                "contrast": round(blob_mean_V - ring_mean_V, 1),
            })

        hair_px = int(cv2.countNonZero(hair_full))
        total_px = hair_full.shape[0] * hair_full.shape[1]
        return {
            "count": len(detections),
            "detections": detections,
            "hair_ratio": round(hair_px / float(total_px), 3),
        }

    @staticmethod
    def draw(frame_bgr: np.ndarray, result: dict) -> np.ndarray:
        out = frame_bgr
        for d in result["detections"]:
            cx = d["x"] + d["w"] // 2
            cy = d["y"] + d["h"] // 2
            r = max(6, int(1.5 * max(d["w"], d["h"])))
            cv2.circle(out, (cx, cy), r, (0, 255, 255), 2)
        label = f"Dandruff: {result['count']}"
        cv2.rectangle(out, (10, 10), (360, 70), (0, 0, 0), -1)
        cv2.putText(out, label, (20, 52), cv2.FONT_HERSHEY_SIMPLEX,
                    1.2, (0, 255, 255), 2, cv2.LINE_AA)
        return out
