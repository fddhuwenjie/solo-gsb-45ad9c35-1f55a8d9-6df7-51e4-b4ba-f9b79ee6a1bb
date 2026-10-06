"use strict";

// ---------------------------------------------------------------------------
// 全局状态
// ---------------------------------------------------------------------------
const ZOOM_LEVELS = [0.5, 0.75, 1, 1.25, 1.5, 2];
let zoomIndex = ZOOM_LEVELS.indexOf(1);

let doc = null;          // { session_id, page_count, pages: [{index,width,height,boxes}] }
const stageEls = [];     // 每页的 DOM 与交互状态

const fileInput = document.getElementById("file-input");
const pagesEl = document.getElementById("pages");
const emptyHint = document.getElementById("empty-hint");
const exportBtn = document.getElementById("export-btn");
const zoomGroup = document.getElementById("zoom-group");
const zoomLabel = document.getElementById("zoom-label");
const banner = document.getElementById("banner");

// ---------------------------------------------------------------------------
// 小工具
// ---------------------------------------------------------------------------
function showBanner(message, kind = "error") {
  banner.textContent = message;
  banner.className = "banner " + (kind === "info" ? "info" : "");
}
function clearBanner() {
  banner.className = "banner hidden";
  banner.textContent = "";
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = `请求失败（HTTP ${res.status}）`;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) { /* 保留默认提示 */ }
    throw new Error(detail);
  }
  return res;
}

// ---------------------------------------------------------------------------
// 渲染文档骨架
// ---------------------------------------------------------------------------
function renderDocument(data) {
  doc = data;
  pagesEl.innerHTML = "";
  stageEls.length = 0;

  data.pages.forEach((page) => {
    const block = document.createElement("section");
    block.className = "page-block";

    const title = document.createElement("div");
    title.className = "page-title";
    const label = document.createElement("span");
    label.textContent = `第 ${page.index + 1} / ${data.page_count} 页 · ${Math.round(page.width)}×${Math.round(page.height)} pt`;
    const clearBtn = document.createElement("button");
    clearBtn.type = "button";
    clearBtn.textContent = "清空本页框";
    title.append(label, clearBtn);

    const stage = document.createElement("div");
    stage.className = "page-stage";

    const img = document.createElement("img");
    img.alt = `第 ${page.index + 1} 页预览`;
    img.draggable = false;

    const canvas = document.createElement("canvas");

    stage.append(img, canvas);
    block.append(title, stage);
    pagesEl.appendChild(block);

    const state = {
      page,
      block,
      stage,
      img,
      canvas,
      ctx: canvas.getContext("2d"),
      drawing: false,
      startPt: null,  // PDF 点坐标
      curPt: null,
    };
    stageEls.push(state);

    attachPointerEvents(state);
    clearBtn.addEventListener("click", async () => {
      if (!state.page.boxes.length) return;
      if (!confirm(`确定删除第 ${page.index + 1} 页上的全部 ${state.page.boxes.length} 个遮盖框吗？`)) return;
      await saveBoxes(page.index, []);
    });
  });

  emptyHint.classList.add("hidden");
  pagesEl.classList.remove("hidden");
  exportBtn.disabled = false;
  zoomGroup.setAttribute("aria-disabled", "false");

  refreshAllPreviews();
}

function pageUrl(pageIndex) {
  return `/api/page/${pageIndex}?zoom=${ZOOM_LEVELS[zoomIndex]}&t=${Date.now()}`;
}

function refreshAllPreviews() {
  zoomLabel.textContent = Math.round(ZOOM_LEVELS[zoomIndex] * 100) + "%";
  for (const state of stageEls) {
    state.img.src = pageUrl(state.page.index);
  }
}

// ---------------------------------------------------------------------------
// 画布叠加层：框始终按 PDF 点坐标存储，绘制时换算到当前显示像素
// ---------------------------------------------------------------------------
function displayScale(state) {
  // 图片的 natural 尺寸是 144dpi * zoom；CSS 显示尺寸可能因布局略小，
  // 用实际客户区宽度换算，因此放大缩小时框仍然对齐同一块内容。
  return state.img.clientWidth / state.page.width;
}

function eventToPt(state, ev) {
  const rect = state.canvas.getBoundingClientRect();
  const s = displayScale(state);
  const x = (ev.clientX - rect.left) / s;
  const y = (ev.clientY - rect.top) / s;
  return [
    Math.max(0, Math.min(state.page.width, x)),
    Math.max(0, Math.min(state.page.height, y)),
  ];
}

function redraw(state) {
  const { ctx, canvas } = state;
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // canvas 像素 / PDF 点（canvas 按 clientWidth*dpr 创建）
  const k = canvas.width / state.page.width;
  ctx.setTransform(k, 0, 0, k, 0, 0); // 之后直接用 PDF 点坐标绘制
  ctx.lineWidth = 2 / dpr;            // 屏幕上始终约 2 CSS 像素
  ctx.strokeStyle = "#ff2d2d";

  const drawRect = (x0, y0, x1, y1) => {
    ctx.strokeRect(x0, y0, x1 - x0, y1 - y0);
  };

  ctx.setLineDash([]);
  for (const [x0, y0, x1, y1] of state.page.boxes) drawRect(x0, y0, x1, y1);

  if (state.drawing && state.startPt && state.curPt) {
    ctx.setLineDash([6 / dpr, 4 / dpr]);
    drawRect(
      state.startPt[0], state.startPt[1],
      state.curPt[0], state.curPt[1],
    );
    ctx.setLineDash([]);
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}

function resizeCanvas(state) {
  if (!state.img.clientWidth) return;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(state.img.clientWidth * dpr);
  const h = Math.round(state.img.clientHeight * dpr);
  if (state.canvas.width !== w || state.canvas.height !== h) {
    state.canvas.width = w;
    state.canvas.height = h;
  }
  redraw(state);
}

function hitTest(state, pt, paddingPt = 6) {
  // 返回最上层（最后画的）命中框下标
  const [px, py] = pt;
  for (let i = state.page.boxes.length - 1; i >= 0; i--) {
    const [x0, y0, x1, y1] = state.page.boxes[i];
    if (px >= x0 - paddingPt && px <= x1 + paddingPt && py >= y0 - paddingPt && py <= y1 + paddingPt) {
      return i;
    }
  }
  return -1;
}

function attachPointerEvents(state) {
  const canvas = state.canvas;
  let downPt = null;
  let moved = false;

  canvas.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    ev.preventDefault();
    canvas.setPointerCapture(ev.pointerId);
    downPt = eventToPt(state, ev);
    moved = false;
    state.startPt = null;
  });

  canvas.addEventListener("pointermove", (ev) => {
    if (!downPt) {
      // 悬停时给命中框一个抓手提示
      const over = hitTest(state, eventToPt(state, ev), 6 / displayScale(state));
      canvas.style.cursor = over >= 0 ? "pointer" : "crosshair";
      return;
    }
    const pt = eventToPt(state, ev);
    const s = displayScale(state);
    if (
      !moved &&
      Math.hypot((pt[0] - downPt[0]) * s, (pt[1] - downPt[1]) * s) >= 4
    ) {
      moved = true;
      state.drawing = true;
      state.startPt = downPt;
      state.stage.classList.add("drawing");
      document.body.classList.add("drawing");
    }
    if (state.drawing) {
      state.curPt = pt;
      redraw(state);
    }
  });

  canvas.addEventListener("pointerup", async (ev) => {
    try {
      if (!downPt) return;
      const upPt = eventToPt(state, ev);
      if (!moved) {
        // 单击：删除最上层命中框
        const idx = hitTest(state, upPt, 6 / displayScale(state));
        if (idx >= 0) {
          const next = state.page.boxes.slice();
          next.splice(idx, 1);
          await saveBoxes(state.page.index, next);
        }
      } else if (state.startPt) {
        const x0 = Math.min(state.startPt[0], upPt[0]);
        const y0 = Math.min(state.startPt[1], upPt[1]);
        const x1 = Math.max(state.startPt[0], upPt[0]);
        const y1 = Math.max(state.startPt[1], upPt[1]);
        // PDF 点单位下小于 2 点的框视为误触
        if (x1 - x0 >= 2 && y1 - y0 >= 2) {
          await saveBoxes(state.page.index, [...state.page.boxes, [x0, y0, x1, y1]]);
        }
      }
      state.drawing = false;
      state.startPt = null;
      state.curPt = null;
      redraw(state);
    } finally {
      downPt = null;
      state.drawing = false;
      state.startPt = null;
      state.curPt = null;
      state.stage.classList.remove("drawing");
      document.body.classList.remove("drawing");
    }
  });

  state.img.addEventListener("load", () => resizeCanvas(state));

  // 窗口 / 容器尺寸变化时重定叠加层（框坐标不变，只是重投影）
  const ro = new ResizeObserver(() => resizeCanvas(state));
  ro.observe(state.img);
}

// ---------------------------------------------------------------------------
// 保存框 -> 服务端规范化 -> 刷新该页位图（预览立即反映遮盖）
// ---------------------------------------------------------------------------
async function saveBoxes(pageIndex, boxes) {
  const state = stageEls[pageIndex];
  try {
    clearBanner();
    const res = await api(`/api/page/${pageIndex}/boxes`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ boxes }),
    });
    const data = await res.json();
    state.page.boxes = data.boxes;
    state.img.src = pageUrl(pageIndex); // 重新渲染含黑色遮盖的位图
    redraw(state);
  } catch (err) {
    showBanner(err.message);
    redraw(state);
  }
}

// ---------------------------------------------------------------------------
// 上传
// ---------------------------------------------------------------------------
fileInput.addEventListener("change", async () => {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;
  clearBanner();

  const form = new FormData();
  form.append("file", file);
  try {
    const res = await api("/api/upload", { method: "POST", body: form });
    const data = await res.json();
    renderDocument(data);
    showBanner(`已打开 “${file.name}”，共 ${data.page_count} 页。`, "info");
  } catch (err) {
    // 上传失败时不替换当前文档；输入框重置以便可以再次选择同名文件
    showBanner(err.message);
  } finally {
    fileInput.value = "";
  }
});

// ---------------------------------------------------------------------------
// 缩放
// ---------------------------------------------------------------------------
document.getElementById("zoom-in").addEventListener("click", () => {
  if (!doc) return;
  zoomIndex = Math.min(ZOOM_LEVELS.length - 1, zoomIndex + 1);
  refreshAllPreviews();
});
document.getElementById("zoom-out").addEventListener("click", () => {
  if (!doc) return;
  zoomIndex = Math.max(0, zoomIndex - 1);
  refreshAllPreviews();
});
document.getElementById("zoom-reset").addEventListener("click", () => {
  if (!doc) return;
  zoomIndex = ZOOM_LEVELS.indexOf(1);
  refreshAllPreviews();
});

window.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") {
    for (const state of stageEls) {
      state.drawing = false;
      state.startPt = null;
      state.curPt = null;
      state.stage.classList.remove("drawing");
      redraw(state);
    }
    document.body.classList.remove("drawing");
  }
});

// ---------------------------------------------------------------------------
// 导出
// ---------------------------------------------------------------------------
exportBtn.addEventListener("click", async () => {
  if (!doc) return;
  clearBanner();
  try {
    const res = await api("/api/export", { method: "POST" });
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "redacted.pdf";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    showBanner(err.message);
  }
});

// ---------------------------------------------------------------------------
// 启动：如果服务端内存里已有会话（页面刷新场景），重新载入
// ---------------------------------------------------------------------------
(async function init() {
  try {
    const res = await fetch("/api/document");
    if (res.ok) renderDocument(await res.json());
  } catch (_) { /* 无会话，显示空状态 */ }
})();
