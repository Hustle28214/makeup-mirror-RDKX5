// Unified makeup-mirror HUD driver.
// Polls /detections.json at ~3 Hz, updates a stack of tiles, and paints
// a rolling sparkline for the "脱妆时间轴" tile.

const $ = (id) => document.getElementById(id);

const $overall  = $("overall");
const $status   = $("status");
const $sym      = $("sym");
const $symDet   = $("sym-detail");
const $blem     = $("blemish");
const $tzone    = $("tzone");
const $dark     = $("dark");
const $darkDet  = $("dark-detail");
const $light    = $("light");
const $lightDet = $("light-detail");
const $drift    = $("drift");
const $spark    = $("spark");

const OVERALL_TEXT = {
  ok:      "整体看起来不错",
  warn:    "有轻微问题，看细项",
  alert:   "多个维度偏差较大",
  unknown: "识别中…",
};

function setState(el, verdict) {
  el.dataset.state = verdict || "unknown";
}

function setRow(name, reg) {
  const row = document.querySelector(`.region-row[data-region="${name}"]`);
  if (!row) return;
  const $s = row.querySelector(".score");
  if (!reg || reg.score == null) {
    row.dataset.state = "unknown"; $s.textContent = "--"; return;
  }
  row.dataset.state = reg.verdict || "unknown";
  $s.textContent = Math.round(reg.score);
}

function paintSpark(points, drift) {
  if (!points || points.length < 2) {
    $spark.innerHTML = "";
    return;
  }
  // points: [{t, s}] where t is seconds-ago (>=0), we want left=old → right=now
  const W = 200, H = 60, PAD = 4;
  const ts = points.map(p => p.t);
  const ss = points.map(p => p.s);
  const tMax = Math.max(...ts);
  const tMin = Math.min(...ts);
  const sMin = 0, sMax = 100;
  const xOf = (t) => {
    // t is "seconds ago", so bigger t → left
    const frac = (tMax === tMin) ? 1 : (tMax - t) / (tMax - tMin);
    return PAD + frac * (W - 2 * PAD);
  };
  const yOf = (s) => {
    const frac = (s - sMin) / (sMax - sMin);
    return H - PAD - frac * (H - 2 * PAD);
  };
  const d = points.map((p, i) => `${i === 0 ? "M" : "L"}${xOf(p.t).toFixed(1)},${yOf(p.s).toFixed(1)}`).join(" ");
  // Baseline line: current - drift
  const last = ss[ss.length - 1];
  let baseline = null;
  if (drift != null) baseline = last - drift;
  const baseD = (baseline != null)
    ? `M${PAD},${yOf(baseline).toFixed(1)} L${W - PAD},${yOf(baseline).toFixed(1)}`
    : "";
  $spark.innerHTML =
    (baseD ? `<path class="base" d="${baseD}"></path>` : "") +
    `<path class="line" d="${d}"></path>`;
}

async function poll() {
  try {
    const r = await fetch("/detections.json", { cache: "no-store" });
    const d = await r.json();

    // -- no face short-circuit
    if (!d.face) {
      setState($("tile-overall"), "unknown");
      $overall.textContent = "--";
      $status.textContent = "未检测到人脸，请靠近镜子";
      ["forehead", "l_cheek", "r_cheek", "chin"].forEach(n => setRow(n, null));

      [["tile-sym", "--"], ["tile-blemish", "--"], ["tile-tzone", "--"],
       ["tile-dark", "--"], ["tile-light", "--"]].forEach(([id]) => {
        setState($(id), "unknown");
      });
      $sym.textContent = "--"; $symDet.textContent = "ΔL / Δa";
      $blem.textContent = "--";
      $tzone.textContent = "--";
      $dark.textContent = "--"; $darkDet.textContent = "L / R";
      $light.textContent = "--"; $lightDet.textContent = "--";
      $drift.textContent = "--";
      $spark.innerHTML = "";
      return;
    }

    // -- overall
    const o = d.overall;
    if (o) {
      setState($("tile-overall"), o.verdict);
      $overall.textContent = Math.round(o.score);
      $status.textContent = OVERALL_TEXT[o.verdict] || "";
    } else {
      setState($("tile-overall"), "unknown");
      $overall.textContent = "--";
      $status.textContent = "皮肤区域太少，正对摄像头试试";
    }

    // -- evenness rows
    const regs = d.regions || {};
    ["forehead", "l_cheek", "r_cheek", "chin"].forEach(n => setRow(n, regs[n]));

    // -- symmetry
    const s = d.symmetry;
    if (s) {
      setState($("tile-sym"), s.verdict);
      $sym.textContent = Math.round(s.score);
      $symDet.textContent = `ΔL ${s.dL.toFixed(1)} / Δa ${s.da.toFixed(1)}`;
    } else {
      setState($("tile-sym"), "unknown");
      $sym.textContent = "--"; $symDet.textContent = "ΔL / Δa";
    }

    // -- blemishes
    const b = d.blemishes || {};
    setState($("tile-blemish"), b.verdict || "unknown");
    $blem.textContent = b.count != null ? b.count : "--";

    // -- t-zone
    const tz = d.tzone || {};
    setState($("tile-tzone"), tz.verdict || "unknown");
    $tzone.textContent = tz.ratio == null ? "--" : `${Math.round(tz.ratio * 100)}%`;

    // -- dark circles
    const dc = d.dark_circles || {};
    setState($("tile-dark"), dc.verdict || "unknown");
    $dark.textContent = dc.score == null ? "--" : dc.score.toFixed(1);
    const dl = dc.sides && dc.sides.left  != null ? dc.sides.left.toFixed(1)  : "--";
    const dr = dc.sides && dc.sides.right != null ? dc.sides.right.toFixed(1) : "--";
    $darkDet.textContent = `L ${dl} / R ${dr}`;

    // -- lighting
    const lt = d.lighting || {};
    setState($("tile-light"), lt.verdict || "unknown");
    if (lt.verdict === "unknown") {
      $light.textContent = "--";
      $lightDet.textContent = "识别中";
    } else {
      $light.textContent = (lt.mean_L != null) ? Math.round(lt.mean_L) : "--";
      const notes = (lt.notes || []).slice(0, 2).join(" · ");
      $lightDet.textContent = notes || `平衡 Δ${lt.balance ?? "--"}`;
    }

    // -- timeline
    const tl = d.timeline || {};
    setState($("tile-timeline"), tl.verdict || "unknown");
    if (tl.drift == null) {
      $drift.textContent = tl.baseline == null ? "校准中" : "0";
    } else {
      $drift.textContent = (tl.drift >= 0 ? "+" : "") + tl.drift.toFixed(1);
    }
    paintSpark(tl.points, tl.drift);

  } catch (e) {
    $status.textContent = "后端未连接";
  }
}
setInterval(poll, 300);
poll();
