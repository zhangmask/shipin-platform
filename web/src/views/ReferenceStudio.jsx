// 参考视频复刻向导 —— AI 从视频站找参考 TVC → 白名单下载 → 切镜/台词/反推
// 提示词 → 预览分镜 → 一键注入画布（/graph?g=…）跑 image_gen/video_gen 复刻。
// 状态流转由后端持久化（data/references/<ref_id>/state.json），前端轮询；
// 帧图/结果均为本地产物，走 blob 拉取（不直接暴露后端路径）。
import React, { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, getApiKey } from "../lib";
import "./reference.css";

const SOURCES = [
  ["bilibili", "B站"], ["youtube", "YouTube"], ["douyin", "抖音"],
];
const PLATFORM = { bilibili: "B站", youtube: "YouTube", douyin: "抖音" };
const POLL_MS = 1500;

const STAGE_TEXT = {
  downloading: "下载中（yt-dlp 抓取参考片，限 mp4）…",
  downloaded: "下载完成，等待启动反推",
  analyzing: "反推中：切镜 → 抽帧 → Whisper 台词 → 逐镜提示词（分钟级）…",
  analyzed: "反推完成",
  error: "任务出错",
};

function fmtDur(sec) {
  if (!sec && sec !== 0) return "";
  const s = Math.round(sec);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** 关键帧图（带鉴权 blob 拉取 + 对象 URL 回收） */
function FrameImg({ rid, name, title }) {
  const [src, setSrc] = useState(null);
  useEffect(() => {
    let alive = true, url = null;
    (async () => {
      try {
        const r = await fetch(`/api/reference/${rid}/frames/${encodeURIComponent(name)}`, {
          headers: getApiKey() ? { "X-API-Key": getApiKey() } : {},
        });
        if (!alive) return;
        if (r.ok) { url = URL.createObjectURL(await r.blob()); setSrc(url); }
      } catch { /* 鉴权降级 */ }
    })();
    return () => { alive = false; if (url) URL.revokeObjectURL(url); };
  }, [rid, name]);
  return src
    ? <img src={src} alt={title || name} />
    : <div className="rs-fnone">{title || name}</div>;
}

/** 候选缩略图（外站图，referrer 置空防防盗链） */
function Thumb({ url, title }) {
  return url
    ? <img src={url} alt={title || ""} loading="lazy" referrerPolicy="no-referrer" />
    : <div className="rs-fnone">无图</div>;
}

export default function ReferenceStudio() {
  const nav = useNavigate();
  // —— 第 1 步：搜索 / 粘贴分享文本 ——
  const [q, setQ] = useState("");
  const [source, setSource] = useState("bilibili");
  const [items, setItems] = useState(null);    // null=未搜；[]=空结果
  const [shareText, setShareText] = useState("");
  // —— 第 2 步：任务状态（轮询） ——
  const [refId, setRefId] = useState(null);
  const [st, setSt] = useState(null);            // GET /api/reference/{id}
  const [polling, setPolling] = useState(false);
  const [pack, setPack] = useState(null);        // 反推包 {script, shots, brief}
  const [expanded, setExpanded] = useState(null); // 展开的分镜 idx
  // —— 注入画布 ——
  const [graphs, setGraphs] = useState([]);
  const [target, setTarget] = useState("");       // "" = 自动新建
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const pollTimer = useRef(null);

  const stopPoll = useCallback(() => {
    if (pollTimer.current) { clearInterval(pollTimer.current); pollTimer.current = null; }
    setPolling(false);
  }, []);

  const loadGraphs = useCallback(async () => {
    try { setGraphs((await api("/api/graphs")).graphs || []); } catch { /* 非阻塞 */ }
  }, []);
  useEffect(() => { loadGraphs(); }, [loadGraphs]);

  // 轮询任务状态：downloaded / analyzed / error 则收敛
  const poll = useCallback((rid) => {
    stopPoll();
    setPolling(true);
    pollTimer.current = setInterval(async () => {
      try {
        const s = await api(`/api/reference/${rid}`);
        setSt(s);
        if (s.stage === "downloaded" || s.stage === "analyzed" || s.stage === "error") {
          stopPoll();
          if (s.stage === "analyzed") {
            const p = await api(`/api/reference/${rid}/pack`);
            setPack(p);
            setMsg(null);
          }
        }
      } catch (e) { stopPoll(); setMsg(`状态查询失败：${e.message}`); }
    }, POLL_MS);
  }, [stopPoll]);
  useEffect(() => () => stopPoll(), [stopPoll]);

  // —— 动作 ——
  const doSearch = async () => {
    if (!q.trim()) return;
    setMsg(null); setBusy(true);
    try {
      const r = await api("/api/reference/search", {
        method: "POST",
        body: JSON.stringify({ q: q.trim(), source, limit: 12 }),
      });
      setItems(r.items || []);
      if (!r.items || !r.items.length) setMsg("没有搜到结果：试试换关键词，或直接粘贴分享链接");
    } catch (e) { setMsg(`搜索失败：${e.message}`); setItems(null); }
    finally { setBusy(false); }
  };

  const doFetch = async (url) => {
    setMsg(null); setBusy(true); setItems(null);
    try {
      const r = await api("/api/reference/fetch", {
        method: "POST",
        body: JSON.stringify({ url, max_duration: 180 }),
      });
      setRefId(r.ref_id); setSt({ stage: "downloading" }); setPack(null);
      poll(r.ref_id);
    } catch (e) { setMsg(e.message); }
    finally { setBusy(false); }
  };

  const doResolve = async () => {
    const t = shareText.trim();
    if (!t) return;
    setMsg(null); setBusy(true);
    try {
      const r = await api("/api/reference/resolve", { method: "POST", body: JSON.stringify({ text: t }) });
      setShareText(r.url);
      await doFetch(r.url);
    } catch (e) { setMsg(`链接解析失败：${e.message}`); }
    finally { setBusy(false); }
  };

  const startAnalyze = async () => {
    if (!refId || busy) return;
    setMsg(null); setBusy(true);
    try {
      await api(`/api/reference/${refId}/analyze`, {
        method: "POST",
        body: JSON.stringify({ scene_threshold: 0.3 }),
      });
      setSt({ stage: "analyzing" });
      poll(refId);
    } catch (e) { setMsg(e.message); }
    finally { setBusy(false); }
  };

  const applyAndGo = async () => {
    if (!refId || busy) return;
    setMsg(null); setBusy(true);
    try {
      const body = target ? { graph_id: target } : {};
      const r = await api(`/api/reference/${refId}/apply`, {
        method: "POST", body: JSON.stringify(body), timeout: 15000,
      });
      nav(`/graph?g=${encodeURIComponent(r.graph_id)}`);
    } catch (e) { setMsg(`注入画布失败：${e.message}`); }
    finally { setBusy(false); }
  };

  const shots = (pack && pack.shots) || [];
  const script = (pack && pack.script) || {};
  const brief = (pack && pack.brief && pack.brief.brief_prefill) || {};
  const vlmUsed = pack && pack.brief && pack.brief.vlm_used;
  const vlmErr = pack && pack.brief && pack.brief.vlm_error;

  return (
    <div className="rs-root">
      <header className="rs-head">
        <b>🎬 参考视频复刻</b>
        <span className="rs-sub">AI 找参考 → 下载 → 抄剧本 · 抄分镜 → 反推提示词 → 注入画布出片</span>
        <button onClick={() => nav("/graph")}>← 节点画布</button>
        <button className="plain" onClick={() => nav("/")}>首页</button>
      </header>

      <div className="rs-body">
        {/* —— 第 1 步：找参考 —— */}
        <section className="rs-col">
          <div className="rs-card">
            <h3>① 找参考视频</h3>
            <div className="rs-row">
              <select value={source} onChange={(e) => setSource(e.target.value)}>
                {SOURCES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
              <input placeholder="关键词：如 咖啡广告 / 手机新品TVC…" value={q}
                     onChange={(e) => setQ(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && doSearch()} />
              <button className="primary" onClick={doSearch} disabled={busy}>搜索</button>
            </div>
            <div className="rs-sep">或粘贴分享链接 / 口令（B站、抖音、YouTube、快手）</div>
            <div className="rs-row">
              <input placeholder="https://… 或 8.88 复制打开…" value={shareText}
                     onChange={(e) => setShareText(e.target.value)}
                     onKeyDown={(e) => e.key === "Enter" && doResolve()} />
              <button onClick={doResolve} disabled={busy || !shareText.trim()}>解析并下载</button>
            </div>
            {msg && <div className="rs-msg err">{msg}</div>}

            <div className="rs-list">
              {!items && !busy && <div className="rs-hint">先搜关键词，或直接粘贴链接。<br />支持 B站 / 抖音 / YouTube（白名单域名，仅 https）。</div>}
              {items && items.length === 0 && <div className="rs-hint">没有结果。</div>}
              {items && items.map((it) => (
                <div key={it.id} className="rs-item">
                  <div className="rs-thumb"><Thumb url={it.thumb} title={it.title} /></div>
                  <div className="rs-item-meta">
                    <div className="rs-item-title" title={it.title}>{it.title}</div>
                    <div className="rs-item-sub">
                      <span>{PLATFORM[it.platform] || it.platform}</span>
                      {it.duration_sec ? <span>{fmtDur(it.duration_sec)}</span> : null}
                    </div>
                  </div>
                  <button disabled={busy} onClick={() => doFetch(it.url)}
                          title="下载参考（≤180s，超时自动截取）">下载</button>
                </div>
              ))}
            </div>
          </div>
        </section>

        {/* —— 第 2 步：任务状态 —— */}
        <section className="rs-col">
          <div className="rs-card">
            <h3>② 解构（下载 → 切镜 → 台词 → 反推）</h3>
            {!refId && <div className="rs-hint">下载参考视频后自动开始，全程后台执行，可离开页面。</div>}
            {refId && (
              <>
                <div className={"rs-stage" + (st && st.stage === "error" ? " err" : "")}>
                  <span className="rs-spin" hidden={!polling && st && st.stage !== "downloaded" && st.stage !== "analyzing"} />
                  {st ? (STAGE_TEXT[st.stage] || st.stage) : "排队中…"}
                  <code>{refId}</code>
                </div>
                {st && st.stage === "downloaded" && (
                  <button className="primary wide" onClick={startAnalyze} disabled={busy}>
                    ⚙ 开始反推（切镜 + 抽帧 + 台词 + 逐镜提示词）
                  </button>
                )}
                {st && st.stage === "error" && (
                  <div className="rs-msg err">{st.error || "任务失败"}</div>
                )}
                {st && st.stage === "analyzed" && !pack && <div className="rs-hint">加载反推包…</div>}
                {pack && (
                  <div className="rs-pack-meta">
                    <div><b>{script.title}</b></div>
                    <div className="rs-item-sub">
                      <span>{script.shot_count || shots.length} 个镜头</span>
                      {script.total_duration ? <span>总长 {fmtDur(script.total_duration)}</span> : null}
                      <span>{vlmUsed ? "多模态反推 ✓" : "反推未启用"}</span>
                    </div>
                    {vlmErr && <div className="rs-msg warn" title={vlmErr}>VLM 未启用：{vlmErr}</div>}
                    {Object.keys(brief).length > 0 && (
                      <div className="rs-brief">{Object.entries(brief).map(([k, v]) => (
                        <span key={k}><b>{k}</b> {String(v).slice(0, 60)}</span>
                      ))}</div>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
          {msg && <div className="rs-msg err">{msg}</div>}
        </section>

        {/* —— 第 3 步：分镜预览 + 注入 —— */}
        <section className="rs-col">
          <div className="rs-card">
            <h3>③ 分镜预览 → 载入画布</h3>
            {!pack && <div className="rs-hint">完成反推后这里会出现逐镜卡片：关键帧 + 台词 + 机位/光影/构图/提示词。</div>}
            {pack && (
              <>
                <div className="rs-shots">
                  {shots.map((s) => (
                    <div key={s.idx} className={"rs-shot" + (expanded === s.idx ? " on" : "")}
                         onClick={() => setExpanded(expanded === s.idx ? null : s.idx)}>
                      <div className="rs-shot-head">
                        <b>#{s.idx}</b>
                        <span>{fmtDur(s.start)}–{fmtDur(s.end)}</span>
                      </div>
                      {s.frames && s.frames[0] && (
                        <FrameImg rid={refId} name={s.frames[0]} title={`镜头${s.idx}`} />
                      )}
                      <div className="rs-shot-tags">
                        {[s.camera, s.lighting, s.tone_and_palette, s.composition].filter(Boolean).join(" · ") || "（无反推，可手改）"}
                      </div>
                      {expanded === s.idx && (
                        <div className="rs-shot-detail">
                          {s.dialogue && <div className="rs-dia">🗣 {s.dialogue}</div>}
                          {s.image_prompt && <div className="rs-probe">图：{s.image_prompt}</div>}
                          {s.video_prompt && <div className="rs-probe">视频：{s.video_prompt}</div>}
                          {s.subject && <div className="rs-probe">主体：{s.subject}</div>}
                        </div>
                      )}
                    </div>
                  ))}
                </div>
                <div className="rs-apply">
                  <label className="rs-target">
                    注入到画布：
                    <select value={target} onChange={(e) => setTarget(e.target.value)}>
                      <option value="">自动新建「复刻-{refId}」</option>
                      {graphs.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
                    </select>
                  </label>
                  <button className="primary big" onClick={applyAndGo} disabled={busy}>
                    ⚡ 载入画布并复刻
                  </button>
                  <div className="rs-tip">将生成 剧本 / 分镜 / 首帧提示词 / 镜头运动 节点链并自动连线，直接「运行全部」即可出片。</div>
                </div>
              </>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}