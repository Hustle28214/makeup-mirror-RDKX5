const $count  = document.getElementById("count");
const $hair   = document.getElementById("hair");
const $fps    = document.getElementById("fps");
const $status = document.getElementById("status");
const $tile   = document.querySelector(".tile");

let last = performance.now();
let ticks = 0;

async function poll() {
  try {
    const r = await fetch("/detections.json", { cache: "no-store" });
    const d = await r.json();
    $count.textContent = d.count;
    $hair.textContent  = `${Math.round((d.hair_ratio || 0) * 100)}%`;

    if (d.count === 0)      { $tile.dataset.state = "ok";    $status.textContent = "未检测到头皮屑"; }
    else if (d.count <= 5)  { $tile.dataset.state = "warn";  $status.textContent = "少量"; }
    else                    { $tile.dataset.state = "alert"; $status.textContent = "较多，建议清洁"; }
  } catch (e) {
    $status.textContent = "后端未连接";
  }
  ticks++;
  const now = performance.now();
  if (now - last >= 1000) {
    $fps.textContent = ticks.toFixed(0);
    ticks = 0;
    last = now;
  }
}
setInterval(poll, 250);
poll();
