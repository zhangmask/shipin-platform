// 过程回放：`/projects/:pid/timeline` —— 整个制作过程一屏看全。
// 阶段里程碑 + 执行事件账本（旧→新）+ 产物资产 + 逐镜 QC + 版本/成本。
import React, { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, KIND_LABEL, fmtMoney, baseName } from "../lib";

const KIND_EMOJI = {
  project_created: "🌱", stage_started: "▶️", stage_recorded: "✅",
  gate_confirmed: "🔓", gate_cleared: "🔓", gate_denied: "🚧",
  downstream_invalidated: "🔁", user_rewritten: "✏️", stage_reset: "♻️",
  phase_started: "🟦", phase_finished: "🟩", budget_set: "💰",
  budget_exceeded: "⛔", preflight_done: "🩺",
};
const STAGE_ORDER = ["brief", "script", "storyboard", "image_prompt",
                     "image_gen", "video_prompt", "video_gen", "post_production"];
const STAGE_LABEL = {
  brief: "简报", script: "剧本", storyboard: "分镜",
  image_prompt: "图生提示词", image_gen: "图片生成",
  video_prompt: "视频提示词", video_gen: "视频生成",
  post_production: "成片",
};
const STAGE_STATUS = {
  PASS: { label: "通过", cls: "tl-ok" },
  FAILED: { label: "未过", cls: "tl-bad" },
  BLOCKED: { label: "阻断", cls: "tl-warn" },
  RUNNING: { label: "运行中", cls: "tl-run" },
};

function fmtTs(ts) {
  if (!ts) return "";
  return String(ts).replace("T", " ").slice(0, 19);
}

export default function Timeline() {
  const { pid } = useParams();
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    (async () => {
      try {
        const r = await api(`/api/pipeline/${encodeURIComponent(pid)}/timeline`);
        setData(r);
      } catch (e) {
        setErr(e.message);
      }
    })();
  }, [pid]);

  if (err) {
    return (
      <main className="main">
        <div className="card">
          <div className="banner err">加载失败: {err}</div>
        </div>
      </main>
    );
  }
  if (!data) return <main className="main"><div className="fade">加载中…</div></main>;

  const stageMap = {};
  data.stages.forEach((s) => { stageMap[s.stage] = s; });
  const byKind = data.cost?.by_kind || {};
  const media = data.assets || [];

  return (
    <main className="main">
      <div className="card">
        <h3 style={{ margin: 0 }}>
          <Link to={`/projects/${encodeURIComponent(pid)}`}>← 看板</Link>
          <span className="mono" style={{ marginLeft: 10 }}>{pid}</span>
          <span className="hint">过程回放：AI 与平台每一步的真实轨迹</span>
        </h3>
      </div>

      {/* 阶段里程碑 */}
      <div className="card">
        <h3>阶段里程碑</h3>
        {data.stages.length === 0 && <div className="fade">尚无阶段记录</div>}
        <div className="row wrap">
          {STAGE_ORDER.filter((s) => stageMap[s]).map((s) => {
            const st = stageMap[s];
            const meta = STAGE_STATUS[String(st.status).toUpperCase()] ||
                         { label: st.status, cls: "tl-mid" };
            return (
              <div key={s} className="stage-chip" title={fmtTs(st.updated_at)}>
                <span className="hint">{STAGE_LABEL[s] || s}</span>
                <b className={meta.cls}>{meta.label}</b>
                <span className="hint mono">
                  {st.artifact_hash ? st.artifact_hash.slice(0, 8) : "—"}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* 执行轨迹 */}
      <div className="card">
        <h3>执行轨迹（{data.events.length} 条）</h3>
        {data.events.length === 0 && <div className="fade">暂无事件</div>}
        <div className="timeline">
          {data.events.map((e) => (
            <div key={e.seq} className="tl-item">
              <div className="tl-head">
                <span className="tl-emoji">
                  {KIND_EMOJI[e.kind] || "•"}
                </span>
                <b>{e.summary}</b>
                <span className="hint mono" style={{ marginLeft: "auto" }}>
                  {fmtTs(e.ts)}
                </span>
              </div>
              <div className="fade">
                {KIND_LABEL[e.kind] || e.kind}
                {e.stage ? ` · ${STAGE_LABEL[e.stage] || e.stage}` : ""}
                {e.detail ? ` · ${e.detail}` : ""}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 产物 */}
      <div className="card">
        <h3>产物（{media.length} 个媒体文件）</h3>
        {media.length === 0 && <div className="fade">暂无媒体产物</div>}
        <div className="row wrap">
          {media.map((a) => (
            <div key={a.name} className="asset-chip" title={`${a.name} · ${a.bytes} B`}>
              <span>{a.kind === "video" ? "🎬" : "🖼️"}</span>
              <span className="mono">{baseName(a.name)}</span>
            </div>
          ))}
        </div>
        {data.preview_frames && data.preview_frames.length > 0 && (
          <div className="row wrap" style={{ marginTop: 8 }}>
            {data.preview_frames.map((u) => (
              <img key={u} src={u} alt="preview"
                   style={{ width: 140, borderRadius: 8,
                            border: "1px solid var(--border)" }} />
            ))}
          </div>
        )}
      </div>

      <div className="row">
        {/* QC 结果 */}
        <div className="card">
          <h3>逐镜 QC 门禁</h3>
          {data.qc.length === 0 && <div className="fade">尚无 QC 记录</div>}
          {data.qc.map((q) => (
            <div key={q.shot_id} className="tl-item">
              <b className={`mt-${q.verdict === "ok" ? "ok" : "bad"}`}>
                {q.verdict === "ok" ? "✅ 通过" : "❌ 拦截"} {q.shot_id}
              </b>
              <span className="fade mono">{fmtTs(q.updated_at)} {q.clip_path}</span>
            </div>
          ))}
        </div>

        {/* 版本与成本 */}
        <div className="card">
          <h3>版本 / 成本</h3>
          <div className="fade">版本索引：
            {Object.entries(data.versions || {}).length === 0
              ? "（无）"
              : Object.entries(data.versions).map(([k, v]) => `${k}×${v}`).join(" · ")}
          </div>
          <div style={{ marginTop: 6 }}>
            总成本: <b>${fmtMoney(data.cost?.total_usd)}</b>
            {data.cost?.records?.length ? `（${data.cost.records.length} 笔）` : ""}
          </div>
          <div className="fade">
            {Object.entries(byKind).map(([k, v]) => `${k}=$${fmtMoney(v)}`).join(" · ")}
          </div>
        </div>
      </div>
    </main>
  );
}