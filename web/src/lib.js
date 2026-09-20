// P5：共享库 —— 常量 / API key 存取 / fetch 封装 / 文本 diff 引擎
export const STAGES = ["brief", "script", "storyboard", "image_prompt",
                       "image_gen", "video_prompt", "video_gen",
                       "post_production"];
export const GATES = [["brief", "简报确认"], ["script", "剧本确认"],
                      ["storyboard", "分镜确认"]];
export const POLL_MS = 5000;
// 中间产物：全部可见；script/storyboard 支持人工改写后重跑
export const ARTIFACTS = [
  ["script", "剧本"], ["storyboard", "分镜"],
  ["image_prompt", "图片提示词"], ["video_prompt", "视频提示词"],
  ["manifest", "生成清单"], ["final_review", "终验报告"],
  ["stitch", "拼接结果"],
];
export const REWRITABLE = new Set(["script", "storyboard"]);
export const KIND_LABEL = {
  project_created: "新建", stage_started: "阶段开始", stage_recorded: "阶段记录",
  gate_confirmed: "闸门确认", downstream_invalidated: "下游失效",
  user_rewritten: "人工改写", gate_cleared: "确认作废", stage_reset: "阶段重置",
  phase_started: "阶段启动", phase_finished: "阶段结束",
  budget_set: "预算设置", budget_exceeded: "预算超限",
  stage_restored: "版本回滚",
};

export const KEY_STORAGE = "shipin.api_key";

export function getApiKey() {
  return localStorage.getItem(KEY_STORAGE) || "";
}
export function setApiKey(k) {
  if (k && k.trim()) localStorage.setItem(KEY_STORAGE, k.trim());
  else localStorage.removeItem(KEY_STORAGE);
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

// 平台调用：自动带 X-API-Key（P0 鉴权）；401/403 抛 ApiError(status=401/403)
export async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const k = getApiKey();
  if (k) headers["X-API-Key"] = k;
  const ctrl = opts.timeout ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), opts.timeout) : null;
  let r;
  try {
    r = await fetch(path, { ...opts, headers, signal: ctrl ? ctrl.signal : undefined });
  } catch (e) {
    if (ctrl && ctrl.signal.aborted) throw new Error("请求超时，服务端仍在处理（画布会自动刷新）");
    throw e;
  } finally {
    if (timer) clearTimeout(timer);
  }
  let body = null;
  try { body = await r.json(); } catch { /* 非 JSON 响应 */ }
  if (r.status === 401 || r.status === 403) {
    const detail = (body && body.detail)
      ? (typeof body.detail === "object"
         ? JSON.stringify(body.detail) : String(body.detail))
      : r.statusText;
    throw new ApiError(`鉴权失败(${r.status})：${detail}`, r.status);
  }
  if (!r.ok) {
    const detail = (body && body.detail)
      ? (typeof body.detail === "object"
         ? JSON.stringify(body.detail) : String(body.detail))
      : r.statusText;
    throw new Error(String(detail));
  }
  return body;
}

export const fmtMoney = (n) => Number(n ?? 0).toFixed(4);
export const baseName = (p) => (p || "").split(/[\\/]/).pop();

// 行级 diff：两端公共前缀/后缀 + 中间整段标 del/add（产物 JSON 都够小）
export function diffLines(oldTxt, newTxt) {
  const a = (oldTxt ?? "").split("\n");
  const b = (newTxt ?? "").split("\n");
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  let j = a.length - 1;
  let k = b.length - 1;
  while (j >= i && k >= i && a[j] === b[k]) { j--; k--; }
  const out = [];
  for (let x = 0; x < i; x++) out.push({ s: "same", t: a[x] });
  for (let x = i; x <= j; x++) out.push({ s: "del", t: a[x] });
  for (let x = i; x <= k; x++) out.push({ s: "add", t: b[x] });
  for (let x = j + 1; x < a.length; x++) out.push({ s: "same", t: a[x] });
  return out;
}