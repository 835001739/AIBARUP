/**
 * AIBAR Bridge —— ComfyUI 前端深链接桥梁。
 *
 * 让 AIBAR 站点（另一个源）能够通过 URL 把「工作流 + 提示词」送进 ComfyUI 画布：
 *
 *   http://127.0.0.1:8188/
 *     ?aibar_wf=<工作流 JSON 的绝对 URL>    必填，需要目标服务允许跨域
 *     &aibar_prompt=<正向提示词>            可选
 *     &aibar_neg=<负向提示词>               可选
 *     &aibar_target=<节点 id>               可选，指定正向提示词写入哪个节点
 *     &aibar_name=<展示名>                  可选
 *
 * 设计约束：
 * - 只调用 ComfyUI 公开的前端 API（app.loadGraphData / app.loadApiJson），不碰私有实现；
 * - 全流程 try/catch：任何一步失败都只影响本次载入，绝不让 ComfyUI 页面挂掉；
 * - 提示词只改 widget 的 value，不改写画布结构。
 */

import { app as appModule } from "/scripts/app.js";

const app = appModule || (typeof window !== "undefined" ? window.app : null);

const PARAM = {
  graph: "aibar_wf",
  prompt: "aibar_prompt",
  negative: "aibar_neg",
  target: "aibar_target",
  name: "aibar_name",
};

// ComfyUI 启动时会自己往画布塞一份默认工作流。扩展的 setup() 跑得比它早，
// 如果这时就 loadGraphData，默认图会在之后把它盖掉（实测：画布退回 7 个默认节点，
// 提示词却被写进了默认图的文本框里，看起来"成功"其实全错）。
// 所以除了等 app.graph 出现，还要等画布签名连续稳定一段时间，并等
// configuringGraphLevel 归零（ComfyUI 用它标记自己正在改画布）。
const SETTLE_STABLE_MS = 260;
const SETTLE_TIMEOUT_MS = 15000;
const RELOAD_RETRY = 8;
const RELOAD_RETRY_GAP_MS = 320;
const TEXT_WIDGET = "text";
const ENCODER_MARK = "CLIPTextEncode";
const SAMPLER_PREFIX = "KSampler";
const POSITIVE_WORDS = ["positive", "正面", "正向"];
const NEGATIVE_WORDS = ["negative", "负面", "负向"];

/* ------------------------------------------------------------------ 提示 */

let _toastHost = null;

function notify(message, severity) {
  severity = severity || "info";
  console.info("[AIBAR Bridge] " + severity + ": " + message);

  // 优先用 ComfyUI 自带 toast（不同版本挂载位置不同，逐个尝试）
  try {
    const candidates = [
      app && app.extensionManager && app.extensionManager.toast,
      app && app.ui && app.ui.toast,
      typeof window !== "undefined" && window.comfyAPI && window.comfyAPI.toast,
    ];
    for (const toast of candidates) {
      if (toast && typeof toast.add === "function") {
        toast.add({
          severity: severity === "error" ? "error" : severity === "warn" ? "warn" : "info",
          summary: "AIBAR",
          detail: message,
          life: severity === "error" ? 12000 : 6000,
        });
        return;
      }
    }
  } catch (err) {
    /* 落到自绘实现 */
  }

  if (typeof document === "undefined") return;
  try {
    if (!_toastHost || !_toastHost.parentNode) {
      _toastHost = document.createElement("div");
      _toastHost.style.cssText =
        "position:fixed;top:16px;right:16px;z-index:99999;display:flex;" +
        "flex-direction:column;gap:8px;max-width:380px;pointer-events:none;";
      document.body.appendChild(_toastHost);
    }
    const color =
      severity === "error" ? "#ff6b6b" : severity === "warn" ? "#f0b429" : "#4caf50";
    const item = document.createElement("div");
    item.style.cssText =
      "pointer-events:auto;background:rgba(24,24,28,.96);color:#f4f4f5;" +
      "border-left:3px solid " + color + ";border-radius:6px;padding:10px 14px;" +
      "font:13px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;" +
      "box-shadow:0 8px 24px rgba(0,0,0,.35);word-break:break-word;";
    item.textContent = message;
    _toastHost.appendChild(item);
    setTimeout(function () {
      if (item.parentNode) item.parentNode.removeChild(item);
    }, severity === "error" ? 12000 : 6000);
  } catch (err) {
    /* 提示失败无所谓 */
  }
}

/* -------------------------------------------------------------- 画布工具 */

function sleep(ms) {
  return new Promise(function (resolve) {
    setTimeout(resolve, ms);
  });
}

/** 画布"指纹"：节点数 + 排序后的节点类型。用来判断画布是否被换掉了。 */
function canvasSignature() {
  const nodes = nodesOf();
  return nodes.length + ":" + nodes.map(nodeType).sort().join("|");
}

/** 待载入工作流的指纹，与 canvasSignature 同一套算法，可直接比较。 */
function graphSignature(uiGraph) {
  const nodes = (uiGraph && uiGraph.nodes) || [];
  return nodes.length + ":" + nodes.map(function (n) { return (n && n.type) || ""; }).sort().join("|");
}

/** ComfyUI 自己正在改画布时为 true（configuringGraphLevel > 0）。 */
function isConfiguring() {
  try {
    return Number(app.configuringGraphLevel) > 0;
  } catch (err) {
    return false;
  }
}

/**
 * 等到 ComfyUI 把画布交给我们为止：
 *   1. app.graph 存在；
 *   2. 不在 configuringGraph 中；
 *   3. 画布指纹连续 SETTLE_STABLE_MS 毫秒没变（默认图已经落定）。
 * 三条都满足才认为"可以安全覆盖画布"。
 */
async function waitForReady() {
  const started = Date.now();
  let last = null;
  let stableSince = 0;

  while (Date.now() - started < SETTLE_TIMEOUT_MS) {
    const hasGraph = nodesOf().length > 0 || !!(app && app.graph);
    if (!hasGraph) {
      last = null;
      stableSince = 0;
      await sleep(120);
      continue;
    }
    if (isConfiguring()) {
      last = null;
      stableSince = 0;
      await sleep(120);
      continue;
    }
    let sig;
    try {
      sig = canvasSignature();
    } catch (err) {
      sig = null;
    }
    if (sig !== last) {
      last = sig;
      stableSince = Date.now();
      await sleep(120);
      continue;
    }
    if (Date.now() - stableSince >= SETTLE_STABLE_MS) return true;
    await sleep(120);
  }
  return false;
}

function nodesOf() {
  try {
    const g = app && app.graph;
    return (g && (g._nodes || g.nodes)) || [];
  } catch (err) {
    return [];
  }
}

function nodeType(node) {
  try {
    return String(
      (node.constructor && node.constructor.type) || node.type || ""
    );
  } catch (err) {
    return "";
  }
}

function textWidget(node) {
  const widgets = (node && node.widgets) || [];
  for (const w of widgets) {
    if (w && w.name === TEXT_WIDGET && typeof w.value === "string") return w;
  }
  return null;
}

/** 尽力复制到剪贴板；浏览器不允许时静默失败，由调用方决定提示文案。 */
function copyToClipboard(text) {
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      navigator.clipboard.writeText(text);
      return true;
    }
  } catch (err) {
    /* 非安全上下文 / 无权限：忽略 */
  }
  return false;
}

/** 解析 target 参数：
 *  "12"     → 节点 12 上名为 text 的 widget（普通画布节点）
 *  "12@text"→ 节点 12 上名为 text 的 widget（子图提升出来的输入框）
 *  "12#0"   → 节点 12 的第 0 个 widget（子图未提升、值只存在于 widgets_values）
 */
function parseTarget(raw) {
  if (raw == null || raw === "") return null;
  const s = String(raw);
  let m = s.match(/^(.+)@(.+)$/);
  if (m) return { id: m[1], widget: m[2] };
  m = s.match(/^(.+)#(\d+)$/);
  if (m) return { id: m[1], index: parseInt(m[2], 10) };
  return { id: s };
}

/** 按 spec 在节点上挑出要写入的 widget，挑不到时退回默认的 text widget。 */
function pickWidget(node, spec) {
  const widgets = (node && node.widgets) || [];
  if (spec && spec.widget) {
    for (const w of widgets) {
      if (w && w.name === spec.widget && typeof w.value === "string") return w;
    }
  }
  if (spec && typeof spec.index === "number") {
    const w = widgets[spec.index];
    if (w && typeof w.value === "string") return w;
  }
  return textWidget(node);
}

function linkOriginId(linkId) {
  if (linkId == null) return null;
  try {
    const links = app.graph.links;
    if (!links) return null;
    const link =
      typeof links.get === "function" ? links.get(linkId) : links[linkId];
    if (!link) return null;
    return link.origin_id != null ? link.origin_id : link.origin;
  } catch (err) {
    return null;
  }
}

/** 沿 KSampler 的 negative 输入连线追溯到负向编码节点。 */
function findNegativeNodeId() {
  for (const node of nodesOf()) {
    if (!String(nodeType(node)).startsWith(SAMPLER_PREFIX)) continue;
    for (const port of node.inputs || []) {
      if (!port || port.name !== "negative") continue;
      const origin = linkOriginId(port.link);
      if (origin != null) return String(origin);
    }
  }
  return null;
}

function pickPositiveNode(negativeId, targetRaw) {
  const nodes = nodesOf();
  const spec = parseTarget(targetRaw);

  if (spec) {
    const hit = nodes.find(function (n) {
      return String(n.id) === String(spec.id) && pickWidget(n, spec);
    });
    if (hit) return hit;
  }

  const withText = nodes.filter(function (n) {
    return !!textWidget(n);
  });
  const encoders = withText.filter(function (n) {
    return nodeType(n).indexOf(ENCODER_MARK) >= 0;
  });
  const candidates = encoders.length ? encoders : withText;
  if (!candidates.length) return null;

  const isNegative = function (n) {
    return negativeId != null && String(n.id) === String(negativeId);
  };

  for (const n of candidates) {
    const title = String(n.title || "").toLowerCase();
    if (!isNegative(n) && POSITIVE_WORDS.some(function (w) {
      return title.indexOf(w) >= 0;
    })) {
      return n;
    }
  }
  const rest = candidates.filter(function (n) {
    return !isNegative(n);
  });
  return rest.length ? rest[0] : null;
}

function pickNegativeNode(negativeId) {
  const nodes = nodesOf();
  if (negativeId != null) {
    const hit = nodes.find(function (n) {
      return String(n.id) === String(negativeId) && textWidget(n);
    });
    if (hit) return hit;
  }
  for (const n of nodes) {
    const title = String(n.title || "").toLowerCase();
    if (textWidget(n) && NEGATIVE_WORDS.some(function (w) {
      return title.indexOf(w) >= 0;
    })) {
      return n;
    }
  }
  return null;
}

function applyText(node, text, spec) {
  const widget = pickWidget(node, spec);
  if (!widget) return false;
  try {
    widget.value = text;
    if (typeof widget.callback === "function") widget.callback(text);
  } catch (err) {
    return false;
  }
  try {
    if (typeof node.computeSize === "function" && typeof node.setSize === "function") {
      node.setSize(node.computeSize());
    }
    if (typeof node.setDirtyCanvas === "function") node.setDirtyCanvas(true, true);
  } catch (err) {
    /* 尺寸重算失败不影响取值 */
  }
  return true;
}

/* ------------------------------------------------------------ 工作流载入 */

/** 取出 UI 格式画布对象（``{nodes, links}``）；不是 UI 格式返回 null。 */
function pickUiGraph(data) {
  if (!data || typeof data !== "object") return null;
  if (Array.isArray(data.nodes)) return data;
  if (data.workflow && Array.isArray(data.workflow.nodes)) return data.workflow;
  return null;
}

/** 取出 API 格式图（``{id: {class_type, inputs}}``）；不是则返回 null。 */
function pickApiGraph(data) {
  if (!data || typeof data !== "object") return null;
  const inner = data.prompt;
  if (inner && typeof inner === "object" && !Array.isArray(inner)) {
    const keys = Object.keys(inner);
    if (keys.length && inner[keys[0]] && inner[keys[0]].class_type) return inner;
  }
  const keys = Object.keys(data);
  if (keys.length && data[keys[0]] && data[keys[0]].class_type) return data;
  return null;
}

async function loadGraph(data) {
  const uiGraph = pickUiGraph(data);
  if (uiGraph) {
    await app.loadGraphData(uiGraph);
    return "ui";
  }
  const apiGraph = pickApiGraph(data);
  if (apiGraph && typeof app.loadApiJson === "function") {
    await app.loadApiJson(apiGraph);
    return "api";
  }
  throw new Error("无法识别的工作流格式（既不是 UI 图也不是 API 图）");
}

/**
 * 载入工作流并**确认它真的留在了画布上**。
 *
 * 单纯 await loadGraphData 是不够的：ComfyUI 启动流程会在之后把默认图盖回来，
 * 而 loadGraphData 不会报错，于是"载入成功"其实是假象。这里载入后比对画布指纹：
 *   - 与目标一致    → 成功；
 *   - 与载入前一致  → 被盖回去了，等一会儿重试；
 *   - 其余不一致    → 部分载入（多半是缺自定义节点），接受并交给上层提示。
 */
async function loadGraphVerified(data) {
  const uiGraph = pickUiGraph(data);
  const expected = uiGraph ? graphSignature(uiGraph) : null;
  let lastErr = null;

  for (let attempt = 1; attempt <= RELOAD_RETRY; attempt++) {
    const before = canvasSignature();
    try {
      await loadGraph(data);
      await sleep(60);
      const after = canvasSignature();
      if (expected && after === expected) {
        return { ok: true, attempts: attempt };
      }
      if (!expected && nodesOf().length > 0) {
        return { ok: true, attempts: attempt };
      }
      if (after !== before) {
        return { ok: true, attempts: attempt, partial: true };
      }
      lastErr = new Error(
        "画布在载入后被回退（第 " + attempt + "/" + RELOAD_RETRY + " 次尝试）"
      );
    } catch (err) {
      lastErr = err;
    }
    await sleep(RELOAD_RETRY_GAP_MS);
  }
  return { ok: false, error: lastErr };
}

/* -------------------------------------------------------------- 主流程 */

let _running = false;

async function run() {
  if (_running) return;
  const params = new URLSearchParams(window.location.search);
  const graphUrl = params.get(PARAM.graph);
  if (!graphUrl) return;
  _running = true;

  const label = params.get(PARAM.name) || "";
  const positive = params.get(PARAM.prompt) || "";
  const negative = params.get(PARAM.negative) || "";
  const target = params.get(PARAM.target) || "";

  notify("正在载入工作流" + (label ? "：" + label : "") + "…");

  // 等 ComfyUI 自己的默认工作流落定再动手，否则会被它盖回来。
  const ready = await waitForReady();
  if (!ready) {
    notify("画布迟迟没有就绪（ComfyUI 仍在初始化），未能载入工作流。请刷新页面重试。", "error");
    return;
  }

  let data;
  try {
    const res = await fetch(graphUrl, { credentials: "omit" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    data = await res.json();
  } catch (err) {
    notify(
      "无法读取工作流 JSON：" + ((err && err.message) || err) +
        "。请确认 AIBAR 站点正在运行且允许跨域。",
      "error"
    );
    return;
  }

  const loaded = await loadGraphVerified(data);
  if (!loaded.ok) {
    notify(
      "工作流载入失败：" + ((loaded.error && loaded.error.message) || loaded.error) +
        "。若反复失败，请刷新页面重试。",
      "error"
    );
    return;
  }
  if (loaded.partial) {
    notify("工作流已载入，但节点数与源文件不一致（可能缺少自定义节点），请检查画布。", "warn");
  }

  const negativeId = findNegativeNodeId();
  const applied = [];

  if (positive) {
    const spec = parseTarget(target);
    const node = pickPositiveNode(negativeId, target);
    if (node && applyText(node, positive, spec)) {
      applied.push("提示词 → " + (node.title || nodeType(node) || ("节点 " + node.id)));
    } else {
      // 子图包装节点的 widget 名对不上时（AIBAR 只能从 JSON 推测），退一步
      // 把提示词放进剪贴板，用户粘贴一次即可，总好过什么都不给。
      copyToClipboard(positive)
        ? notify("工作流已载入：该节点的文本框没能自动定位，提示词已复制到剪贴板，请手动粘贴。", "warn")
        : notify("工作流已载入，但没找到可写入提示词的文本节点，请手动粘贴。", "warn");
    }
  }

  if (negative) {
    const node = pickNegativeNode(negativeId);
    if (node && applyText(node, negative)) {
      applied.push("负向提示词 → " + (node.title || nodeType(node) || ("节点 " + node.id)));
    }
  }

  try {
    if (app.graph && typeof app.graph.setDirtyCanvas === "function") {
      app.graph.setDirtyCanvas(true, true);
    }
  } catch (err) {
    /* 忽略 */
  }

  notify(
    "已载入工作流" + (label ? "：" + label : "") +
      (applied.length ? "，" + applied.join("；") : "") + "。可直接点“运行”。",
    "info"
  );
}

if (app && typeof app.registerExtension === "function") {
  app.registerExtension({
    name: "AIBAR.Bridge",
    setup() {
      // 延后一帧，避免与 ComfyUI 自身启动流程抢占主线程
      setTimeout(run, 0);
    },
  });
} else {
  console.warn("[AIBAR Bridge] 未找到 app，扩展未注册");
}
