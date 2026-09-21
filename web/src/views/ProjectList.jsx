// P5：`/` 项目列表页 —— 原左栏 + 新建项目 + 平台成本总览（P3）
import React, { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, POLL_MS, getApiKey } from "../lib";

export default function ProjectList() {
  const nav = useNavigate();
  const [projects, setProjects] = useState([]);
  const [seal, setSeal] = useState(null);
  const [health, setHealth] = useState(null);
  const [banner, setBanner] = useState(null);
  const [newForm, setNewForm] = useState({});
  const [busy, setBusy] = useState(false);
  const [globalCosts, setGlobalCosts] = useState(null);
  const [globalBudgetDraft, setGlobalBudgetDraft] = useState("");
  const [hasKey, setHasKey] = useState(!!getApiKey());

  const refreshAll = useCallback(async () => {
    try {
      setProjects((await api("/api/projects")).projects || []);
    } catch (e) {
      setBanner({ kind: e.status ? "err" : "err",
                  text: `项目列表加载失败: ${e.message}` });
    }
    try {
      setHealth(await api("/api/health"));
      setSeal(await api("/api/platform/integrity"));
    } catch (_) { /* 非阻塞 */ }
  }, []);

  const loadGlobalCosts = useCallback(async () => {
    try {
      setGlobalCosts(await api("/api/platform/costs", { method: "GET" }));
    } catch (e) {
      setGlobalCosts({ denied: true, detail: e.message });
    }
  }, []);

  useEffect(() => { refreshAll(); loadGlobalCosts(); }, [refreshAll, loadGlobalCosts]);

  useEffect(() => {
    const t = setInterval(() => refreshAll(), POLL_MS);
    return () => clearInterval(t);
  }, [refreshAll]);

  const makeProject = async () => {
    const brief = {};
    if (newForm.product_info) brief.product_info = newForm.product_info;
    if (newForm.tone) brief.tone = newForm.tone;
    if (newForm.duration_sec) brief.duration_sec = Number(newForm.duration_sec);
    if (newForm.content_type) brief.content_type = newForm.content_type;
    if (!brief.product_info) {
      setBanner({ kind: "warn", text: "至少填一句产品/需求描述" });
      return;
    }
    const pid = (newForm.project_id || `p${Date.now().toString(36)}`).trim();
    const payload = { project_id: pid, brief };
    const budget = Number(newForm.max_budget_usd);
    if (newForm.max_budget_usd && Number.isFinite(budget) && budget > 0) {
      payload.max_budget_usd = budget;
    }
    setBusy(true);
    setBanner(null);
    try {
      await api("/api/pipeline/text", { method: "POST",
                                        body: JSON.stringify(payload) });
      setBanner({ kind: "ok", text: `项目 ${pid} 已创建，brief→剧本→分镜完成` });
      setNewForm({});
      await refreshAll();
      nav(`/projects/${encodeURIComponent(pid)}`);
    } catch (e) {
      setBanner({ kind: "err", text: e.message });
    } finally {
      setBusy(false);
    }
  };

  const saveGlobalBudget = async () => {
    const v = globalBudgetDraft.trim();
    const payload = v === "" ? { max_monthly_usd: null }
                             : { max_monthly_usd: Number(v) };
    if (v !== "" && !Number.isFinite(payload.max_monthly_usd)) {
      setBanner({ kind: "err", text: "预算必须是数字（留空=解除）" });
      return;
    }
    try {
      await api("/api/platform/budget",
                { method: "POST", body: JSON.stringify(payload) });
      setBanner({ kind: "ok", text: "全局月度预算已更新" });
      setGlobalBudgetDraft("");
      await loadGlobalCosts();
    } catch (e) {
      setBanner({ kind: "err", text: `预算更新失败: ${e.message}` });
    }
  };

  return (
    <div className="app">
      <aside className="side">
        <div className="side-head">
          <h1>Shipin Platform</h1>
          <div className="sub">POST /api · 受控编排 · P5 看板</div>
          {health && (
            <div className="fade" style={{ marginTop: 3 }}>
              v{health.version} · AGNES{" "}
              {health.agnes && health.agnes.key_configured ? "已配置" : "未配置"}
            </div>
          )}
          <span className={`seal ${seal ? (seal.ok ? "okk" : "bad") : ""}`}>
            <span className="dot" />
            {seal
              ? (seal.ok ? `封印通过 · ${seal.clean} 文件` : "封印被篡改！")
              : "封印加载中…"}
          </span>
          <div className="row" style={{ marginTop: 10 }}>
            <button className={hasKey ? "" : "primary"}
                    onClick={() => nav("/login")}>
              {hasKey ? "切换 API Key" : "登录 API Key"}
            </button>
            <button className="primary" onClick={() => nav("/graph")}>
              ⚡ 节点画布
            </button>
            <button className="primary" onClick={() => nav("/ref")}>
              🎬 参考复刻
            </button>
          </div>
        </div>

        <div className="projects">
          <div className="card" style={{ padding: 12 }}>
            <h3 style={{ marginBottom: 6 }}>新建项目</h3>
            <label>项目 ID（留空自动生成）</label>
            <input placeholder="my-ads-001"
                   value={newForm.project_id || ""}
                   onChange={(e) =>
                     setNewForm({ ...newForm, project_id: e.target.value })} />
            <label>一句话需求 *</label>
            <textarea placeholder="产品：深蓝莓咖啡，15 秒带货短视频，冷峻说服感"
                      value={newForm.product_info || ""}
                      onChange={(e) =>
                        setNewForm({ ...newForm,
                                     product_info: e.target.value })} />
            <div className="row" style={{ marginTop: 8 }}>
              <select value={newForm.content_type || ""}
                      onChange={(e) =>
                        setNewForm({ ...newForm,
                                     content_type: e.target.value })}>
                <option value="">类型…</option>
                <option>抖音</option><option>小红书</option><option>发布会</option>
              </select>
              <input type="number" min="5" max="120" placeholder="时长(秒)"
                     style={{ width: 110 }}
                     value={newForm.duration_sec || ""}
                     onChange={(e) =>
                       setNewForm({ ...newForm,
                                    duration_sec: e.target.value })} />
            </div>
            <input placeholder="基调：说服冷峻 / 温暖 / 高燃…"
                   style={{ marginTop: 8 }}
                   value={newForm.tone || ""}
                   onChange={(e) =>
                     setNewForm({ ...newForm, tone: e.target.value })} />
            <input type="number" min="0" step="0.001"
                   placeholder="预算上限 USD（可空）" style={{ marginTop: 8 }}
                   value={newForm.max_budget_usd || ""}
                   onChange={(e) =>
                     setNewForm({ ...newForm,
                                  max_budget_usd: e.target.value })} />
            <button className="primary" style={{ width: "100%", marginTop: 12 }}
                    disabled={busy} onClick={makeProject}>
              {busy ? "后台响应中…" : "创建 → AI 生成全链路"}
            </button>
          </div>

          <div className="fade" style={{ margin: "8px 2px" }}>
            项目（{projects.length}）
          </div>
          {projects.map((p) => (
            <div key={p.project_id}
                 className="proj"
                 onClick={() => nav(`/projects/${encodeURIComponent(p.project_id)}`)}>
              <div className="id">{p.project_id}</div>
              <div className="meta">
                {p.created_at ? new Date(p.created_at).toLocaleString() : ""}
                {" · "}PASS {p.passed_stages}{" · "}latest {p.latest_stage}
              </div>
            </div>
          ))}
          {projects.length === 0 && <div className="fade">暂无项目</div>}
        </div>
      </aside>

      <main className="main">
        {banner && <div className={`banner ${banner.kind}`}>{banner.text}</div>}
        <div className="empty" style={{ marginTop: "8vh" }}>
          ← 左侧新建或选择项目
          <div className="fade" style={{ marginTop: 8 }}>
            流程：新建（AI 全链路）→ 看板确认闸门 → 阶段二生成 → 阶段三成片 →
            回滚/改写 → 重跑。全过程事件留痕、成本记账、版本可回滚。
          </div>
        </div>

        {globalCosts && (
          <div className="card" style={{ marginTop: 20 }}>
            <h3>
              平台成本总览
              <span className="hint">P3 FinOps：跨项目聚合（OpenCost 式 showback）</span>
              {globalCosts.exceeded && (
                <span className="step BLOCKED" style={{ marginLeft: 8 }}>
                  全局月度预算超限——生成类接口将 422
                </span>
              )}
            </h3>
            {globalCosts.denied ? (
              <div className="fade">
                需要 admin 权限：{globalCosts.detail}
              </div>
            ) : (
              <div className="row" style={{ flexWrap: "wrap", gap: 16 }}>
                <div><b>全平台</b> ${globalCosts.total_usd}</div>
                <div>
                  <b>本月</b> ${globalCosts.month_usd}
                  {globalCosts.global_budget?.max_monthly_usd != null && (
                    <span className="hint">
                      / 上限 ${globalCosts.global_budget.max_monthly_usd}
                    </span>
                  )}
                </div>
                <div><b>近 7 日</b> ${globalCosts.week_usd}</div>
                {globalCosts.exceeded && (
                  <div className="banner err" style={{ margin: 0 }}>
                    全局月度预算超限（超 ${globalCosts.overrun_usd}）——
                    生成类接口将被 422
                  </div>
                )}
                {globalCosts.top_projects?.length > 0 && (
                  <div>
                    <b>Top 项目</b>
                    {globalCosts.top_projects.slice(0, 4).map((p) => (
                      <span key={p.project_id} className="mono hint"
                            style={{ marginLeft: 8 }}>
                        {p.project_id}: ${p.usd}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
            <div className="row" style={{ marginTop: 8 }}>
              <input
                placeholder="全局月度预算 USD（留空=解除）"
                value={globalBudgetDraft}
                onChange={(e) => setGlobalBudgetDraft(e.target.value)}
              />
              <button onClick={saveGlobalBudget}>设置全局预算</button>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}