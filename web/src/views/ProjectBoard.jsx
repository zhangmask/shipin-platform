// P5：`/projects/:pid` 看板 —— 阶段状态 / 闸门 / 产物编辑 / 版本 diff /
// 事件瀑布 / 成本 / 预览。原有单页能力全量保留并拆路由。
import React, { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import {
  api, STAGES, GATES, ARTIFACTS, REWRITABLE, KIND_LABEL, POLL_MS,
  fmtMoney, baseName, diffLines,
} from "../lib";

export default function ProjectBoard() {
  const { pid } = useParams();
  const [report, setReport] = useState(null);
  const [status, setStatus] = useState(null);
  const [events, setEvents] = useState([]);
  const [artifact, setArtifact] = useState(null);
  const [editText, setEditText] = useState({});
  const [previews, setPreviews] = useState([]);
  const [budgetDraft, setBudgetDraft] = useState("");
  const [banner, setBanner] = useState(null);
  const [busy, setBusy] = useState(false);
  const [task, setTask] = useState(null);
  const [versions, setVersions] = useState({});
  const [diffView, setDiffView] = useState(null);

  const refreshProject = useCallback(async () => {
    try {
      const [rep, st, ev, pv] = await Promise.all([
        api(`/api/pipeline/${encodeURIComponent(pid)}/report`),
        api(`/api/project/${encodeURIComponent(pid)}/status`),
        api(`/api/pipeline/${encodeURIComponent(pid)}/events?limit=30`),
        api(`/api/pipeline/${encodeURIComponent(pid)}/preview`),
      ]);
      setReport(rep);
      setStatus(st);
      setEvents(ev.events || []);
      setPreviews(pv.frames || []);
    } catch (e) {
      setBanner({ kind: "err", text: `项目详情加载失败: ${e.message}` });
    }
  }, [pid]);

  useEffect(() => {
    setArtifact(null);
    setTask(null);
    setDiffView(null);
    refreshProject();
    const t = setInterval(refreshProject, POLL_MS);
    return () => clearInterval(t);
  }, [pid, refreshProject]);

  // P2 版本索引（回滚 / diff 数据源）
  const loadVersions = useCallback(async () => {
    try {
      const r = await api(`/api/pipeline/${encodeURIComponent(pid)}/versions`);
      setVersions(r.versions || {});
    } catch { /* 增强能力，失败不打断 */ }
  }, [pid]);

  const loadArtifact = async (stage) => {
    try {
      const a = await api(
        `/api/pipeline/${encodeURIComponent(pid)}/artifact?stage=${stage}`);
      setArtifact(a);
      if (a.exists && a.content != null) {
        setEditText((prev) => ({
          ...prev, [stage]: JSON.stringify(a.content, null, 2) }));
      } else if (!a.exists) {
        setEditText((prev) => ({ ...prev, [stage]: "" }));
      }
      if (REWRITABLE.has(stage)) await loadVersions();
    } catch (e) {
      setBanner({ kind: "err", text: `产物读取失败: ${e.message}` });
    }
  };

  const restoreVersion = async (stage) => {
    if (!versions[stage] || versions[stage].length < 2) return;
    const prev = versions[stage][versions[stage].length - 2];
    if (!window.confirm(
        `回滚 ${stage} 到 v${prev.version}？\n将清空确认并失效下游阶段。`)) {
      return;
    }
    try {
      const r = await api(`/api/pipeline/${encodeURIComponent(pid)}/restore`,
                          { method: "POST",
                            body: JSON.stringify({ stage, version: prev.version }) });
      setBanner({ kind: "ok",
                  text: `已回滚到 v${prev.version}，下游 ${r.invalidated} 阶段失效——请重新确认后重跑` });
      await refreshProject();
      await loadArtifact(stage);
    } catch (e) {
      setBanner({ kind: "err", text: `回滚失败: ${e.message}` });
    }
  };

  // P5：版本 diff（新旧 JSON 行级对比）
  const openDiff = async (stage) => {
    const list = versions[stage];
    if (!list || list.length < 2) return;
    const prev = list[list.length - 2];
    const head = list[list.length - 1];
    try {
      const [p, h] = await Promise.all([
        api(`/api/pipeline/${encodeURIComponent(pid)}/versions/${stage}/${prev.version}`),
        api(`/api/pipeline/${encodeURIComponent(pid)}/versions/${stage}/${head.version}`),
      ]);
      setDiffView({
        stage,
        fromVersion: p.version,
        toVersion: h.version,
        lines: diffLines(JSON.stringify(p.content, null, 2),
                         JSON.stringify(h.content, null, 2)),
      });
    } catch (e) {
      setBanner({ kind: "err", text: `版本对比失败: ${e.message}` });
    }
  };

  const act = async (path, body, successMsg) => {
    setBusy(true);
    setBanner(null);
    try {
      const r = await api(path, { method: "POST",
                                  body: JSON.stringify(body) });
      setBanner({ kind: "ok", text: successMsg });
      await refreshProject();
      return r;
    } catch (e) {
      setBanner({ kind: "err", text: e.message });
      return null;
    } finally {
      setBusy(false);
    }
  };

  const doConfirm = (gate) => act(
    "/api/project/confirm",
    { project_id: pid, gate, approved_by: "user", note: "web 看板确认" },
    `闸门 ${gate} 已确认`,
  );

  // P1：长任务走 async 车道 + 进度条
  // 轮68:摊成逐镜画布——管线产物 → 每镜可编辑节点(一句话/景别/运动/
  // 转场/引擎/语速),改完在画布上重跑,不用回管线。
  const openCanvas = async () => {
    setBanner(null);
    try {
      const r = await api(
        `/api/graphs/from-project/${encodeURIComponent(pid)}?aspect=portrait`,
        { method: "POST" });
      if (!r || !r.id) throw new Error(r?.detail || "导出失败");
      navigate(`/graph?g=${encodeURIComponent(r.id)}`);
    } catch (e) {
      setBanner({ kind: "err", text: `导出画布失败: ${e.message || e}` });
    }
  };

  const runStageAsync = async (label, url) => {    setBanner(null);
    try {
      const r = await api(url + "?async=true",
                          { method: "POST",
                            body: JSON.stringify({ project_id: pid }) });
      if (!r || !r.task_id) throw new Error("服务端未返回 task_id");
      setTask({ task_id: r.task_id, label, progress: r.progress ?? 0,
                status: r.status, current_stage: r.current_stage || "" });
      let t = r;
      while (!["success", "failed"].includes(t.status)) {
        await new Promise((res) => setTimeout(res, 1000));
        t = await api(`/api/tasks/${r.task_id}`, { method: "GET" });
        setTask({ ...t, label });
      }
      setBanner({ kind: t.status === "success" ? "ok" : "err",
                  text: `${label} ${t.status === "success" ? "完成" : "失败"}` +
                        (t.progress ? `（${t.progress}%）` : "") +
                        (t.error ? ` — ${t.error}` : "") });
      await refreshProject();
    } catch (e) {
      setBanner({ kind: "err", text: e.message });
      setTask(null);
    }
  };

  const saveRewrite = async (stage) => {
    const raw = editText[stage] || "";
    let content = null;
    try {
      content = JSON.parse(raw);
    } catch {
      setBanner({ kind: "err", text: `${stage} 内容不是合法 JSON，未保存` });
      return;
    }
    const r = await act(
      `/api/pipeline/${encodeURIComponent(pid)}/rewrite`,
      { stage, content },
      `${stage} 已按你的版本改写：确认已作废` +
        (r && r.invalidated ? `，下游 ${r.invalidated} 阶段待重跑` : "") +
        "。请重新确认后跑阶段二");
    if (r) {
      await loadArtifact(stage);
      await loadVersions();
    }
  };

  const saveBudget = async () => {
    const v = budgetDraft.trim();
    const value = v === "" ? null : Number(v);
    if (v !== "" && (!Number.isFinite(value) || value < 0)) {
      setBanner({ kind: "err", text: "预算必须是 ≥0 的数字（留空=解除）" });
      return;
    }
    const r = await act(
      `/api/pipeline/${encodeURIComponent(pid)}/budget`,
      { max_budget_usd: value },
      value == null ? "预算上限已解除" : `预算上限已设为 $${value}`);
    if (r) setBudgetDraft("");
  };

  const stages = (status && status.stages) || (report && report.stages) || {};
  const confirmations = (status && status.confirmations)
                      || (report && report.confirmations) || {};
  const artifacts = status && status.artifacts;   // P4 富化：产物清单含版本数
  const costs = report && report.costs;
  const manifest = report && report.manifest;
  const budget = report && report.budget;
  const overBudget = budget && budget.max_budget_usd != null
                     && costs && costs.total_usd > budget.max_budget_usd;

  const shotRows = manifest && manifest.shots
    ? (Array.isArray(manifest.shots)
       ? manifest.shots
       : Object.entries(manifest.shots).map(([sid, sc]) => (
           { shot_id: sid, ...(sc && typeof sc === "object" ? sc : {}) })))
    : [];

  return (
    <main className="main">
      {banner && <div className={`banner ${banner.kind}`}>{banner.text}</div>}

      <div className="card row spread">
        <div>
          <h3 style={{ margin: 0 }}>
            <Link to="/">← 项目列表</Link>
            <span className="mono" style={{ marginLeft: 10 }}>{pid}</span>
            {report && report.reference_id && (
              <span className="hint">参考视频: {report.reference_id}</span>
            )}
          </h3>
        </div>
        <div className="row">
          <button className="primary" disabled={busy}
                  onClick={() => runStageAsync("阶段二 · 生成", "/api/pipeline/generate")}>
            阶段二 · 生成（首帧/视频/TTS）
          </button>
          <button disabled={busy}
                  onClick={() => runStageAsync("阶段三 · 成片", "/api/pipeline/assemble")}>
            阶段三 · 成片
          </button>
          <button disabled={busy} title="摊成画布：每镜一句话/景别/运动/转场全部可改，改完重跑"
                  onClick={openCanvas}>
            🎬 逐镜画布编辑
          </button>
          <Link className="btn"
                to={`/projects/${encodeURIComponent(pid)}/timeline`}>
            过程回放 ↗
          </Link>
        </div>
      </div>

      {task && ["queued", "running", "retry"].includes(task.status) && (
        <div className="card">
          <h3>
            {task.label} 执行中
            <span className="hint">{task.current_stage || task.status} · {task.progress ?? 0}%</span>
          </h3>
          <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
            <div style={{ flex: 1, height: 12, borderRadius: 6,
                          background: "#2a2a3a", overflow: "hidden" }}>
              <div style={{ width: `${Math.max(2, task.progress ?? 0)}%`,
                            height: "100%",
                            background: "linear-gradient(90deg,#22d3ee,#3b82f6)",
                            transition: "width .6s ease" }} />
            </div>
            <span className="mono">{task.task_id.slice(0, 8)}</span>
          </div>
        </div>
      )}

      {Object.keys(stages).length > 0 && (
        <div className="card">
          <h3>
            阶段状态
            <span className="hint">花钱步骤前必须 script/storyboard 双确认且前置 PASS</span>
          </h3>
          <div className="row">
            {STAGES.map((st) => (
              <span key={st}
                    className={`step ${stages[st] ? stages[st].status : ""}`}>
                {st}
              </span>
            ))}
          </div>
          {artifacts && (
            <div className="fade" style={{ marginTop: 8 }}>
              产物快照（P2 版本化）：{
                artifacts.filter((a) => a.versions > 0)
                  .map((a) => `${a.stage} v${a.head_version}`)
                  .join(" · ") || "尚无快照（跑过文案阶段后出现）"
              }
            </div>
          )}
        </div>
      )}

      <div className="card">
        <h3>人工确认闸门</h3>
        <div className="row">
          {GATES.map(([gate, label]) => {
            const c = confirmations[gate];
            return (
              <button key={gate} disabled={busy || !!c}
                      className={c ? "" : "primary"}
                      onClick={() => doConfirm(gate)}>
                {label} · {c ? `已确认(${c.approved_by})` : "未确认"}
              </button>
            );
          })}
        </div>
      </div>

      {/* C5/P5: 中间产物 —— 内容可见、可改写重跑、可对比版本 */}
      <div className="card">
        <h3>
          中间产物
          <span className="hint">
            剧本/分镜可改写保存→确认作废→下游重跑（先确认再跑阶段二）
          </span>
        </h3>
        <div className="row" style={{ flexWrap: "wrap" }}>
          {ARTIFACTS.map(([key, label]) => (
            <button key={key}
                    className={artifact && artifact.stage === key ? "primary" : ""}
                    onClick={() => loadArtifact(key)}>
              {label}
            </button>
          ))}
        </div>
        {artifact && (
          <div style={{ marginTop: 10 }}>
            <div className="row">
              <div className="fade">
                {artifact.exists
                  ? `${artifact.file} · ${artifact.content ? "已生成" : "解析失败"}`
                  : `${artifact.file} 尚未生成（对应阶段跑完后可见）`}
              </div>
              {/* P5：版本 diff 入口 —— 需要 ≥2 个历史版本（P2 快照） */}
              <button disabled={!versions[artifact.stage]
                                || versions[artifact.stage].length < 2}
                      onClick={() => openDiff(artifact.stage)}>
                对比上一版
                <span className="hint" style={{ marginLeft: 4 }}>
                  {versions[artifact.stage]?.length || 0} 版
                </span>
              </button>
            </div>
            {diffView && diffView.stage === artifact.stage && (
              <div className="diff-box">
                <div className="fade" style={{ marginBottom: 4 }}>
                  对比 v{diffView.fromVersion} → v{diffView.toVersion}
                  （红=删除行 绿=新增行）
                </div>
                <pre className="diff-pre">
                  {diffView.lines.map((ln, i) => (
                    <div key={i} className={`diff-line diff-${ln.s}`}>
                      <span className="diff-mark">{ln.s === "del" ? "−"
                                             : ln.s === "add" ? "+" : " "}</span>
                      {ln.t || " "}
                    </div>
                  ))}
                </pre>
              </div>
            )}
            {REWRITABLE.has(artifact.stage) && artifact.exists ? (
              <>
                <textarea
                  spellCheck="false" rows={14}
                  style={{ width: "100%", fontFamily: "monospace",
                           fontSize: 12, marginTop: 6 }}
                  value={editText[artifact.stage] || ""}
                  onChange={(e) => setEditText({
                    ...editText, [artifact.stage]: e.target.value })}
                />
                <div className="row" style={{ marginTop: 6 }}>
                  <button className="primary" disabled={busy}
                          onClick={() => saveRewrite(artifact.stage)}>
                    保存改写 → 失效下游并置 PENDING
                  </button>
                  <button disabled={busy || versions[artifact.stage]?.length < 2}
                          onClick={() => restoreVersion(artifact.stage)}>
                    回滚到上一版本
                  </button>
                </div>
              </>
            ) : (
              <pre className="fade mono" style={{ whiteSpace: "pre-wrap",
                                                  maxHeight: 360,
                                                  overflow: "auto" }}>
                {artifact.content != null
                 ? JSON.stringify(artifact.content, null, 2)
                 : artifact.parse_error || "—"}
              </pre>
            )}
          </div>
        )}
      </div>

      {/* P5: 事件瀑布 —— 时间线式执行轨迹（AI 调用自动留痕） */}
      <div className="card">
        <h3>
          执行轨迹 <span className="hint">时间线 · AI 经 MCP/API 调用自动留痕</span>
        </h3>
        {events.length === 0 && <div className="fade">暂无事件</div>}
        <div className="timeline">
          {events.map((ev) => (
            <div className="tl-item" key={ev.seq}>
              <div className="tl-dot" />
              <div className="tl-body">
                <div className="row" style={{ gap: 8 }}>
                  <span className={`step ${ev.kind === "budget_exceeded" ? "BLOCKED" : "PASS"}`}
                        style={{ padding: "1px 6px" }}>
                    {KIND_LABEL[ev.kind] || ev.kind}
                  </span>
                  <span className="fade">
                    {new Date(ev.ts).toLocaleString()}
                  </span>
                </div>
                <div style={{ marginTop: 2 }}>{ev.summary}</div>
                {ev.detail && <div className="fade">{ev.detail}</div>}
              </div>
            </div>
          ))}
        </div>
      </div>

      {costs && (
        <div className="card">
          <h3>
            成本记账{" "}
            <span className="hint">USD · providers.json 定价 · 实时</span>
            {overBudget && (
              <span className="step BLOCKED" style={{ marginLeft: 8 }}>
                预算超限，生成已硬停
              </span>
            )}
          </h3>
          <div className="row" style={{ marginBottom: 8, flexWrap: "wrap" }}>
            <div className="cost-total">${fmtMoney(costs.total_usd)}</div>
            <div className="row" style={{ marginLeft: 12 }}>
              <input type="number" min="0" step="0.001"
                     placeholder="预算上限 USD（留空=解除）" style={{ width: 180 }}
                     value={budgetDraft}
                     onChange={(e) => setBudgetDraft(e.target.value)} />
              <button disabled={busy} onClick={saveBudget}>设置预算</button>
              <span className="fade">
                {budget && budget.max_budget_usd != null
                 ? `上限 $${budget.max_budget_usd} · ${
                     Math.round(costs.total_usd / budget.max_budget_usd * 100)}%`
                 : "未设上限"}
              </span>
            </div>
          </div>
          <table style={{ flex: 1, minWidth: 420 }}>
            <thead>
              <tr><th>类型</th><th>模型</th><th>单位</th>
                  <th>USD</th><th>备注</th></tr>
            </thead>
            <tbody>
              {(costs.records || []).slice(-6).reverse().map((c, i) => (
                <tr key={i}>
                  <td>{c.kind}</td>
                  <td className="num">{c.model}</td>
                  <td className="num">{c.units}</td>
                  <td className="num">{fmtMoney(c.usd)}</td>
                  <td className="fade">{c.note}</td>
                </tr>
              ))}
              {(costs.records || []).length === 0 && (
                <tr><td colSpan="5" className="fade">尚未产生成本</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}

      {previews.length > 0 && (
        <div className="card">
          <h3>
            成片预览 <span className="hint">assemble 后自动抽帧</span>
          </h3>
          <div className="row" style={{ flexWrap: "wrap" }}>
            {previews.map((u) => (
              <img key={u} src={u} alt="preview frame"
                   style={{ maxHeight: 160, marginRight: 8,
                            border: "1px solid #333", borderRadius: 4 }} />
            ))}
          </div>
        </div>
      )}

      {manifest && (
        <div className="card">
          <h3>生成清单 <span className="hint">首尾帧链式策略 / 逐镜 QC</span></h3>
          <table>
            <thead><tr><th>镜头</th><th>策略</th><th>clip</th><th>QC</th><th>时长</th></tr></thead>
            <tbody>
              {shotRows.map((sc) => (
                <tr key={sc.shot_id || sc.shotIndex}>
                  <td className="num">{sc.shot_id || sc.shotIndex}</td>
                  <td>{sc.strategy || "—"}</td>
                  <td className="num">{baseName(sc.clip)}</td>
                  <td>{sc.qc || sc.verdict || "—"}</td>
                  <td className="num">{sc.duration_sec ?? "—"}</td>
                </tr>
              ))}
              {shotRows.length === 0 && (
                <tr><td colSpan="5" className="fade">尚未生成</td></tr>
              )}
            </tbody>
          </table>
          {manifest.output && (
            <div className="fade mono" style={{ marginTop: 8 }}>
              成片: {manifest.output}
            </div>
          )}
        </div>
      )}

      {costs && (
        <div className="card">
          <h3>速览 <span className="hint">受控入口 · 旧 /api/pipeline/run 已 410 废弃</span></h3>
          <div className="fade mono" style={{ whiteSpace: "pre-wrap" }}>
{`POST /api/pipeline/text       阶段一：brief→剧本→分镜（服务端审核循环）
POST /api/project/confirm      人工闸门：brief / script / storyboard
POST /api/pipeline/generate    阶段二：首帧→链式锚定视频→逐镜 QC→TTS
POST /api/pipeline/assemble    阶段三：转场→字幕→声音→终验→RELEASED
GET  /api/pipeline/{id}/report          快照 + 成本 + 预算
GET  /api/pipeline/{id}/events          执行轨迹（AI 调用自动留痕）
GET  /api/pipeline/{id}/artifact?stage=… 中间产物内容
POST /api/pipeline/{id}/rewrite         人工改写 → 失效下游 → 重跑
POST /api/pipeline/{id}/restore {stage,version}  版本回滚
GET  /api/pipeline/{id}/versions/{stage}/{ver}  版本内容（diff 数据源）
POST /api/pipeline/{id}/preflight       只读体检（凭据/产物/闸门/预算）`}
          </div>
        </div>
      )}
    </main>
  );
}