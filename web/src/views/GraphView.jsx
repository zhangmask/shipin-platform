// 节点画布（ComfyUI 风格手操编排）
//  - 无限画布：滚轮缩放（0.35~1.8）、拖空白/右键平移、Shift 拖拽框选、多选
//  - 组件库侧栏 + 双击空白/搜索面板添加节点（过滤/键盘选择）
//  - 右键菜单：节点（运行/重命名/删除）与空白（运行全部/适配/新建）与连线（删除）
//  - 连线 = 真实数据流：按住输出端口拖到输入端口，输入输出同 kind 才可连
//    （拖线时高亮可连端口、禁用不可连端口），前后端双重校验
//  - 多选：Shift/Ctrl 点击节点、框选；拖动任一选中节点整组移动
//  - 键盘：Delete 删选中、Ctrl+A 全选、Esc 取消、F 适配视图、R 重命名选中
//  - 运行全部（Queue Prompt 语义）：拓扑序逐节点执行，review/qc 门照常生效
//  - 双击节点标题重命名
//  - SSE 事件流：外部 AI 调平台 API 时节点状态实时推送
import React, { useEffect, useRef, useState } from "react";
import { api, getApiKey } from "../lib";
import "./graph.css";

const NODE_W = 236;              // 与 .gv-node 宽度一致
const HEADER_H = 44;             // 节点标题行高
const PORT_STEP = 26;            // 多端口垂直间距

function uid(prefix) {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return `${prefix}_${crypto.randomUUID().slice(0, 8)}`;
  }
  return `${prefix}_${Date.now().toString(36)}_${Math.floor(Math.random() * 1e4)}`;
}

export default function GraphView() {
  const [defs, setDefs] = useState(null);
  const [graphs, setGraphs] = useState([]);
  const [cur, setCur] = useState(null);
  const [sel, setSel] = useState(null);            // 单选节点 id
  const [multi, setMulti] = useState([]);          // 多选节点 id[]
  const [wireSel, setWireSel] = useState([]);      // 选中的连线下标[]
  const [marquee, setMarquee] = useState(null);    // 框选 {x0,y0,x1,y1} 屏幕坐标
  const [err, setErr] = useState(null);
  const [view, setView] = useState({ x: 60, y: 40, z: 1 });
  const [wiring, setWiring] = useState(null);      // {fromId, fromPort, kind}
  const [busy, setBusy] = useState(null);          // "node_id" | "__all__" | null
  const [live, setLive] = useState({});            // SSE 运行中 {node_id: true}
  const [palette, setPalette] = useState(null);
  const [palQ, setPalQ] = useState("");
  const [palIdx, setPalIdx] = useState(0);
  const [menu, setMenu] = useState(null);          // {x, y, nodeId?|wireIdx?}
  const [editing, setEditing] = useState(null);    // 正在重命名的节点 id
  const canvasRef = useRef(null);
  const dragState = useRef(null);                  // {mode, ...}
  const saveTimer = useRef(null);
  const wiringTo = useRef(null);
  const reloadTimer = useRef(null);
  const palInput = useRef(null);

  const loadDefs = async () => {
    setDefs((await api("/api/graphs/kit/definitions")).nodes);
  };
  const refreshList = async () => {
    setGraphs((await api("/api/graphs")).graphs || []);
  };
  const loadGraph = async (gid) => {
    const r = await api(`/api/graphs/${gid}`);
    setCur(r.graph);
    if (r.errors && r.errors.length) setErr(`画布校验：${r.errors.join("; ")}`);
    else setErr(null);
  };
  const scheduleReload = () => {
    clearTimeout(reloadTimer.current);
    reloadTimer.current = setTimeout(() => {
      if (cur && cur.id) loadGraph(cur.id);
    }, 200);
  };

  useEffect(() => { loadDefs(); refreshList(); }, []);

  // ---- SSE ----
  useEffect(() => {
    if (!cur || !cur.id) return;
    const es = new EventSource(`/api/graphs/${cur.id}/events`);
    es.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch { return; }
      if (ev.type === "node" && ev.status) {
        if (ev.status === "running") {
          setLive((p) => ({ ...p, [ev.node_id]: true }));
        } else {
          setLive((p) => { const q = { ...p }; delete q[ev.node_id]; return q; });
          scheduleReload();
        }
        return;
      }
      if (ev.type === "run" || ev.type === "run_all") {
        if (ev.node_id) setLive((p) => ({ ...p, [ev.node_id]: true }));
        return;
      }
      if (ev.type === "node" && ev.op) scheduleReload();
      if (ev.type === "changed") scheduleReload();
    };
    return () => es.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cur && cur.id]);

  // ---- 键盘（Graph 快捷键） ----
  useEffect(() => {
    const onKey = (e) => {
      const tag = (e.target && e.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      if (e.key === "Escape") {
        setMarquee(null); setWiring(null); setMenu(null); setPalette(null);
        setEditing(null); setSel(null); setMulti([]); setWireSel([]);
        return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a") {
        if (cur) { e.preventDefault(); setMulti(cur.nodes.map((n) => n.id)); }
        return;
      }
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        if (wireSel.length) {
          mutate((g) => { g.edges = g.edges.filter((_, i) => !wireSel.includes(i)); });
          setWireSel([]);
          return;
        }
        const ids = new Set([...(multi || []), ...(sel ? [sel] : [])]);
        if (ids.size) {
          mutate((g) => {
            g.nodes = g.nodes.filter((n) => !ids.has(n.id));
            g.edges = g.edges.filter((e2) => !ids.has(e2.from) && !ids.has(e2.to));
          });
          setSel(null); setMulti([]);
        }
        return;
      }
      if (e.key === "f" || e.key === "F") { fitView(); return; }
      if ((e.key === "r" || e.key === "R") && sel) renameNode(sel);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cur, sel, multi, wireSel]);

  // ---- 保存 ----
  const saveGraph = (g) => {
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(async () => {
      try {
        const r = await api(`/api/graphs/${g.id}`, {
          method: "PUT",
          body: JSON.stringify({ name: g.name, nodes: g.nodes, edges: g.edges }),
        });
        if (r && r.errors && r.errors.length) setErr(r.errors.join("; "));
      } catch (e) { setErr(`保存失败：${e.message}`); }
    }, 300);
  };
  const mutate = (fn) => {
    const g = JSON.parse(JSON.stringify(cur));
    fn(g);
    setCur(g);
    saveGraph(g);
  };

  // ---- 坐标 ----
  const toWorld = (clientX, clientY) => {
    const r = canvasRef.current.getBoundingClientRect();
    return { x: (clientX - r.left - view.x) / view.z,
             y: (clientY - r.top - view.y) / view.z };
  };
  const toCanvas = (clientX, clientY) => {
    const r = canvasRef.current.getBoundingClientRect();
    return { x: clientX - r.left, y: clientY - r.top };
  };

  // ---- 画布：左键/中键空白 ----
  const onCanvasMouseDown = (e) => {
    if (e.button === 2) return;   // 右键交给 contextmenu
    if (e.target !== e.currentTarget) return;
    // 在空白处落点：取消未完成的连线拖拽
    if (wiring) { setWiring(null); wiringTo.current = null; }
    if (e.shiftKey) {
      e.preventDefault();
      const p = toCanvas(e.clientX, e.clientY);
      setMarquee({ x0: p.x, y0: p.y, x1: p.x, y1: p.y });
      dragState.current = { mode: "marquee", sx: p.x, sy: p.y };
      return;
    }
    e.preventDefault();
    dragState.current = { mode: "pan", sx: e.clientX, sy: e.clientY };
  };
  const onCanvasMouseMove = (e) => {
    const ds = dragState.current;
    if (ds && ds.mode === "pan") {
      setView((v) => ({ ...v,
        x: v.x + (e.clientX - ds.sx), y: v.y + (e.clientY - ds.sy) }));
      ds.sx = e.clientX; ds.sy = e.clientY;
    } else if (ds && ds.mode === "marquee") {
      const p = toCanvas(e.clientX, e.clientY);
      setMarquee((m) => (m ? { ...m, x1: p.x, y1: p.y } : m));
    } else if (ds && ds.mode === "node") {
      const w = toWorld(e.clientX, e.clientY);
      const ox = w.x - ds.wx, oy = w.y - ds.wy;   // 当前相对开始位置的偏移
      mutate((g) => {
        for (const [id, orig] of Object.entries(ds.origins)) {
          const n = g.nodes.find((x) => x.id === id);
          if (n) { n.x = Math.round(orig.x + ox); n.y = Math.round(orig.y + oy); }
        }
      });
    }
    if (wiring) {
      wiringTo.current = toWorld(e.clientX, e.clientY);
    }
  };
  const onCanvasMouseUp = (e) => {
    const ds = dragState.current;
    if (ds && ds.mode === "marquee" && cur) {
      // 框选落定：矩形（画布坐标）与节点包围盒相交即选中
      const p = toCanvas(e.clientX, e.clientY);
      const x0 = Math.min(ds.sx, p.x), x1 = Math.max(ds.sx, p.x);
      const y0 = Math.min(ds.sy, p.y), y1 = Math.max(ds.sy, p.y);
      const picked = [];
      for (const n of cur.nodes) {
        const nx = view.x + n.x * view.z;
        const ny = view.y + n.y * view.z;
        const nw = NODE_W * view.z, nh = 150 * view.z;
        if (nx < x1 && nx + nw > x0 && ny < y1 && ny + nh > y0) picked.push(n.id);
      }
      if (picked.length) {
        if (e.shiftKey || e.ctrlKey) {
          setMulti((m) => [...new Set([...m, ...picked])]);
        } else { setMulti(picked); setSel(null); }
      } else if (!e.shiftKey && !e.ctrlKey) {
        setMulti([]); setSel(null);
      }
    }
    dragState.current = null;
    setMarquee(null);
  };
  const onCanvasDoubleClick = (e) => {
    if (e.target.closest(".gv-node, .gv-wires")) return;
    setSel(null); setMulti([]);
    const p = toCanvas(e.clientX, e.clientY);
    setPalette({ x: p.x, y: p.y });
    setPalQ(""); setPalIdx(0);
  };
  const onWheel = (e) => {
    e.preventDefault();
    const r = canvasRef.current.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    setView((v) => {
      const nz = Math.min(1.8, Math.max(0.35, v.z * (e.deltaY < 0 ? 1.12 : 0.89)));
      return { z: nz,
               x: mx - (mx - v.x) * (nz / v.z),
               y: my - (my - v.y) * (nz / v.z) };
    });
  };

  // ---- 节点拖拽（整组） ----
  const onStartNodeDrag = (e, n) => {
    if (e.button !== 0) return;
    if (e.target.closest("input,textarea,select,button,.gv-port,.gv-menu,.gv-title-edit"))
      return;
    e.preventDefault(); e.stopPropagation();
    const inMulti = multi.includes(n.id);
    if (!inMulti && !e.shiftKey && !e.ctrlKey) setSel(n.id);
    const ids = inMulti ? multi : [n.id];
    const w = toWorld(e.clientX, e.clientY);
    const orig = {};
    for (const id of ids) {
      const m = cur.nodes.find((x) => x.id === id);
      if (m) orig[id] = { x: m.x, y: m.y };
    }
    dragState.current = { mode: "node", ids: Object.keys(orig),
                          orig, wx: w.x, wy: w.y };
  };

  // ---- 连线 ----
  const onPortDown = (e, node, portName, kind) => {
    e.preventDefault(); e.stopPropagation();
    setSel(node.id);
    setWiring({ fromId: node.id, fromPort: portName, kind });
    wiringTo.current = portPoint(node, true, portName);
  };
  const onPortUp = (e, node, portName, kind) => {
    const w = wiring;
    e.preventDefault(); e.stopPropagation();
    setWiring(null); wiringTo.current = null;
    if (!w) return;
    if (w.fromId === node.id) return;              // 自连禁止
    if (w.kind !== kind) { setErr(`端口类型不匹配：${w.kind} ≠ ${kind}`); return; }
    mutate((g) => {
      const dup = g.edges.find((ed) => ed.from === w.fromId && ed.to === node.id);
      if (dup) { dup.to_port = portName; dup.from_port = w.fromPort; return; }
      g.edges.push({ from: w.fromId, from_port: w.fromPort,
                     to: node.id, to_port: portName, order: g.edges.length });
    });
  };
  const toggleWire = (i) => setWireSel((w) => w.includes(i) ? w.filter((x) => x !== i) : [...w, i]);
  const removeEdge = (i) => { mutate((g) => { g.edges.splice(i, 1); }); setWireSel([]); };

  // ---- 运行 ----
  const runNode = async (nodeId) => {
    if (!cur || busy) return;
    setBusy(nodeId); setErr(null);
    setLive((p) => ({ ...p, [nodeId]: true }));
    try {
      await api(`/api/graphs/${cur.id}/run`, {
        method: "POST",
        body: JSON.stringify({ node_id: nodeId, force: false }),
        timeout: 600000,
      });
      await loadGraph(cur.id);
    } catch (e) {
      setErr(`运行失败：${e.message}`);
      await loadGraph(cur.id);
    } finally {
      setBusy(null);
      setLive((p) => { const q = { ...p }; delete q[nodeId]; return q; });
    }
  };
  const runAll = async () => {
    if (!cur || busy) return;
    setBusy("__all__"); setErr(null);
    try {
      const r = await api(`/api/graphs/${cur.id}/run-all`, {
        method: "POST",
        body: JSON.stringify({ force: false }),
        timeout: 900000,
      });
      const nodes = (r.results && r.results.nodes) || {};
      const fails = Object.values(nodes).filter((x) => !x.ok);
      if (fails.length) {
        setErr(`运行完成，${fails.length} 个节点未通过：` +
               fails.slice(0, 3).map((x) => String(x.error || "").slice(0, 60)).join("；"));
      } else setErr(null);
      await loadGraph(cur.id);
    } catch (e) {
      setErr(`运行全部失败：${e.message}`);
      await loadGraph(cur.id);
    } finally {
      setBusy(null);
      setLive({});
    }
  };
  const fitView = () => {
    if (!cur || !cur.nodes.length || !canvasRef.current) return;
    const r = canvasRef.current.getBoundingClientRect();
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    cur.nodes.forEach((n) => {
      x0 = Math.min(x0, n.x); y0 = Math.min(y0, n.y);
      x1 = Math.max(x1, n.x + NODE_W); y1 = Math.max(y1, n.y + 120);
    });
    const z = Math.min(1.2, Math.max(0.35,
      Math.min((r.width - 80) / (x1 - x0 || 1), (r.height - 80) / (y1 - y0 || 1))));
    setView({ z, x: 40 - x0 * z, y: 40 - y0 * z });
  };

  // ---- 增删改 ----
  const addNodeAt = (type, wx, wy) => {
    const d = defs[type];
    const params = {};
    for (const p of (d.params || [])) {
      if (p.schema && typeof p.schema.value !== "undefined") params[p.key] = p.schema.value;
    }
    const id = uid(type);
    mutate((g) => g.nodes.push({ id, type, x: Math.round(wx), y: Math.round(wy), params }));
    setSel(id);
    return id;
  };
  const newNode = (type) => {
    if (!cur) { setErr("请先选择或新建画布"); return; }
    const n = cur.nodes.length;
    addNodeAt(type, 90 + (n % 4) * 90 + 20 * (n % 2), 60 + (n % 5) * 58);
  };
  const removeNode = (id) => mutate((g) => {
    g.nodes = g.nodes.filter((n) => n.id !== id);
    g.edges = g.edges.filter((e) => e.from !== id && e.to !== id);
  });
  const renameNode = (id) => { setSel(id); setEditing(id); };
  const commitRename = (id, title) => {
    mutate((g) => {
      const m = g.nodes.find((x) => x.id === id);
      if (!m) return;
      if (title && title.trim()) m.title = title.trim();
      else delete m.title;
    });
    setEditing(null);
  };
  const updateParam = (nodeId, key, val) => mutate((g) => {
    const n = g.nodes.find((x) => x.id === nodeId);
    if (n) n.params[key] = val;
  });

  const newGraph = async () => {
    const name = prompt("画布名称", `画布 ${graphs.length + 1}`);
    if (!name) return;
    const r = await api("/api/graphs", { method: "POST", body: JSON.stringify({ name }) });
    await refreshList();
    await loadGraph(r.id);
  };
  const deleteGraph = async () => {
    if (!cur || !window.confirm(`删除画布「${cur.name}」及全部产物？`)) return;
    await api(`/api/graphs/${cur.id}`, { method: "DELETE" });
    setCur(null);
    await refreshList();
  };

  // ---- 端口位置（SVG 端点 + 鼠标跟随共用） ----
  const portIdx = (n, isOut, portName) => {
    const d = defs[n.type];
    const list = isOut ? (d.outputs || []) : (d.inputs || []);
    const i = list.findIndex((p) => p.name === portName);
    return i < 0 ? 0 : i;
  };
  const portPoint = (n, isOut, portName) => ({
    x: n.x + (isOut ? NODE_W + 4 : -4),
    y: n.y + HEADER_H + 20 + portIdx(n, isOut, portName) * PORT_STEP,
  });

  // ---- 搜索面板 ----
  const palList = defs ? Object.keys(defs).filter((k) => {
    const d = defs[k];
    const q = palQ.trim().toLowerCase();
    if (!q) return true;
    return `${d.label} ${k} ${d.category || ""}`.toLowerCase().includes(q);
  }) : [];
  const pickPal = () => {
    if (!palette) return;
    const k = palList[palIdx];
    if (!k) return;
    const wx = (palette.x - view.x) / view.z - NODE_W / 2;
    const wy = (palette.y - view.y) / view.z - 40;
    addNodeAt(k, wx, wy);
    setPalette(null);
  };

  // ---- 右键菜单 ----
  const openMenu = (e, nodeId) => {
    e.preventDefault(); e.stopPropagation();
    setMenu({ ...toCanvas(e.clientX, e.clientY), nodeId });
  };
  const closeMenu = () => setMenu(null);

  // 编入醒目集合（正在连线时，可连 / 不可连端口标记）
  const isWiring = !!wiring;

  return (
    <div className="gv-root" onMouseDown={menu ? closeMenu : undefined}>
      <div className="gv-topbar">
        <b>节点画布</b>
        <select value={cur ? cur.id : ""}
                onChange={(e) => { const v = e.target.value; if (v) { setSel(null); setCur(null); loadGraph(v); } }}>
          <option value="">选择画布…</option>
          {graphs.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
        </select>
        <button onClick={newGraph}>＋ 新建画布</button>
        <button onClick={deleteGraph} disabled={!cur}>删除画布</button>
        <button onClick={() => setPalette({ x: 420, y: 120 })} disabled={!cur}>⊕ 添加节点</button>
        <button onClick={runAll} disabled={!cur || !!busy}
                title="拓扑序运行全部节点（审核/质检门照常生效）">▶ 运行全部</button>
        <button onClick={fitView} disabled={!cur} title="适配视图（F）">⛶ 适配</button>
        <span className="gv-tip">
          双击空白加节点 · Shift+拖框选 · 拖空平移 · Delete 删选中 · Ctrl+A 全选 · Esc 取消 · R 改名
        </span>
        {err && <span className="gv-err">{err}</span>}
      </div>

      <div className="gv-main">
        <aside className="gv-libs">
          <div className="gv-libs-title">组件库（单击添加）</div>
          {defs && Object.keys(defs).map((k) => {
            const d = defs[k];
            return (
              <button key={k} className="gv-lib" style={{ borderColor: d.color }}
                      title={d.hint || ""} onClick={() => newNode(k)}>
                <span className="gv-libdot" style={{ background: d.color }} />
                {d.label}
                <small>{d.category}</small>
              </button>
            );
          })}
          {!defs && <span className="gv-libs-load">组件库加载中…</span>}
        </aside>

        <div className="gv-canvas"
             ref={canvasRef}
             onMouseDown={onCanvasMouseDown}
             onMouseMove={onCanvasMouseMove}
             onMouseUp={onCanvasMouseUp}
             onMouseLeave={onCanvasMouseUp}
             onWheel={onWheel}
             onDoubleClick={onCanvasDoubleClick}
             onContextMenu={(e) => { if (e.target === e.currentTarget) openMenu(e, null); }}>

          <svg className="gv-wires"
               style={{
                 position: "absolute", left: 0, top: 0, width: "100%", height: "100%",
                 transform: `translate(${view.x}px, ${view.y}px) scale(${view.z})`,
                 transformOrigin: "0 0", overflow: "visible", pointerEvents: "none",
               }}>
            {cur && cur.edges.map((e, ei) => {
              const f = cur.nodes.find((n) => n.id === e.from);
              const t = cur.nodes.find((n) => n.id === e.to);
              if (!f || !t || !defs[f.type] || !defs[t.type]) return null;
              const p1 = portPoint(f, true, e.from_port);
              const p2 = portPoint(t, false, e.to_port);
              const bend = Math.max(48, Math.abs(p2.x - p1.x) * 0.55);
              const d = `M${p1.x} ${p1.y} C${p1.x + bend} ${p1.y}, ${p2.x - bend} ${p2.y}, ${p2.x} ${p2.y}`;
              return (
                <g key={`${e.from}_${e.to}_${ei}`}
                   className={"gv-wireg" + (wireSel.includes(ei) ? " sel" : "")}
                   style={{ pointerEvents: "stroke" }}>
                  <path d={d} className="gv-wire"
                        onClick={(ev) => { ev.stopPropagation(); toggleWire(ei); }}
                        onContextMenu={(ev) => { ev.preventDefault(); ev.stopPropagation();
                          setMenu({ ...toCanvas(ev.clientX, ev.clientY), wireIdx: ei }); }} />
                </g>
              );
            })}
            {wiring && (() => {
              const f = cur && cur.nodes.find((n) => n.id === wiring.fromId);
              if (!f || !defs[f.type]) return null;
              const p1 = portPoint(f, true, wiring.fromPort);
              const p2 = wiringTo.current || p1;
              const d = `M${p1.x} ${p1.y} C${p1.x + 70} ${p1.y}, ${p2.x - 70} ${p2.y}, ${p2.x} ${p2.y}`;
              return <path d={d} className="gv-wire temp" />;
            })()}
          </svg>

          {marquee && (
            <div className="gv-marquee" style={{
              left: Math.min(marquee.x0, marquee.x1),
              top: Math.min(marquee.y0, marquee.y1),
              width: Math.abs(marquee.x1 - marquee.x0),
              height: Math.abs(marquee.y1 - marquee.y0),
            }} />
          )}

          {!cur && (
            <div className="gv-empty">
              点左上角「＋ 新建画布」或选择已有画布。<br />
              节点 = 真实服务端执行：剧本 → 分镜 → 首尾帧提示词 → 生图/生视频 →
              QC → 审核 → 拼接出片 → 成品卡片；连线 = 产物真实流入下游。
            </div>
          )}

          {cur && cur.nodes.map((n) => {
            const d = defs[n.type];
            if (!d) return null;
            const isRun = busy === n.id || live[n.id];
            const isSel = sel === n.id || multi.includes(n.id);
            const ins = d.inputs || [];
            const outs = d.outputs || [];
            return (
              <div key={n.id}
                   className={"gv-node" + (isSel ? " sel" : "") + (isRun ? " run" : "")}
                   style={{ left: n.x * view.z + view.x, top: n.y * view.z + view.y }}
                   onMouseDown={(e) => onStartNodeDrag(e, n)}
                   onClick={(e) => {
                     e.stopPropagation();
                     if (e.shiftKey || e.ctrlKey) {
                       setMulti((m) => m.includes(n.id) ? m.filter((x) => x !== n.id) : [...m, n.id]);
                     } else { setSel(n.id); }
                   }}
                   onContextMenu={(e) => openMenu(e, n.id)}>
                <div className="gv-node-head" style={{ background: d.color }}>
                  {editing === n.id ? (
                    <input className="gv-title-edit" defaultValue={n.title || d.label}
                           autoFocus onFocus={(e) => e.target.select()}
                           onClick={(e) => e.stopPropagation()}
                           onBlur={(e) => commitRename(n.id, e.target.value)}
                           onKeyDown={(e) => {
                             if (e.key === "Enter") commitRename(n.id, e.target.value);
                             if (e.key === "Escape") setEditing(null);
                           }} />
                  ) : (
                    <span className="gv-node-title" title="双击重命名"
                          onDoubleClick={(e) => { e.stopPropagation(); renameNode(n.id); }}>
                      {n.title || d.label}
                    </span>
                  )}
                  <span className="gv-node-tools">
                    <NodeBadge node={n} />
                    {isRun ? <span className="gv-spin" />
                      : <button className="gv-run" title="运行（先跑上游）"
                                onClick={(e) => { e.stopPropagation(); runNode(n.id); }}>▶</button>}
                    <button className="gv-del" title="删除节点"
                            onClick={(e) => { e.stopPropagation(); removeNode(n.id); }}>×</button>
                  </span>
                </div>
                <div className="gv-ports">
                  <span className="gv-in-row">
                    {ins.map((p, pi) => {
                      const inType = p.kind;
                      const ok = isWiring && wiring.kind === inType;
                      return (
                        <span key={p.name}
                              className={"gv-port in" + (isWiring ? (ok ? " hot" : " cold") : "")}
                              title={`${p.label}${p.required ? "（必连）" : ""}${isWiring ? (ok ? " — 可连" : " — 类型不符") : ""}`}
                              onMouseUp={(e) => onPortUp(e, n, p.name, inType)}>
                          <i className="gv-dotc" style={{ background: d.color }} />
                          <em>{p.label}</em>
                        </span>
                      );
                    })}
                  </span>
                  <span className="gv-out-row">
                    {outs.map((p, pi) => (
                      <span key={p.name} className="gv-port out" title={p.label}
                            onMouseDown={(e) => onPortDown(e, n, p.name, p.kind)}>
                        <em>{p.label}</em><i className="gv-dotc" style={{ background: d.color }} />
                      </span>
                    ))}
                  </span>
                </div>
                <div className="gv-params">
                  {(d.params || []).map((p) => (
                    <ParamRow key={p.key} p={p}
                              value={n.params?.[p.key]}
                              onChange={(v) => updateParam(n.id, p.key, v)} />
                  ))}
                </div>
                <NodeOut node={n} gid={cur.id} def={d} />
              </div>
            );
          })}

          {palette && (
            <div className="gv-palette" style={{ left: palette.x, top: palette.y }}
                 onMouseDown={(e) => e.stopPropagation()}>
              <input ref={palInput} autoFocus placeholder="搜索节点…（↑↓ 选择，Enter 添加）"
                     value={palQ}
                     onChange={(e) => { setPalQ(e.target.value); setPalIdx(0); }}
                     onKeyDown={(e) => {
                       if (e.key === "Escape") setPalette(null);
                       else if (e.key === "ArrowDown") { e.preventDefault(); setPalIdx((i) => Math.min(i + 1, palList.length - 1)); }
                       else if (e.key === "ArrowUp") { e.preventDefault(); setPalIdx((i) => Math.max(i - 1, 0)); }
                       else if (e.key === "Enter") { e.preventDefault(); pickPal(); }
                     }} />
              <div className="gv-pal-list">
                {palList.length === 0 && <div className="gv-pal-empty">没有匹配的节点</div>}
                {palList.map((k, i) => {
                  const d = defs[k];
                  return (
                    <div key={k}
                         className={"gv-pal-item" + (i === palIdx ? " on" : "")}
                         onMouseEnter={() => setPalIdx(i)}
                         onClick={() => { setPalIdx(i); pickPal(); }}>
                      <span className="gv-libdot" style={{ background: d.color }} />
                      <em>{d.label}</em>
                      <small>{d.category}</small>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {menu && (
            <div className="gv-menu" style={{ left: menu.x, top: menu.y }}
                 onMouseDown={(e) => e.stopPropagation()}>
              {menu.nodeId ? (
                <>
                  <div className="gv-menu-title">
                    {cur.nodes.find((n) => n.id === menu.nodeId)?.title ||
                     defs[cur.nodes.find((n) => n.id === menu.nodeId)?.type]?.label}
                  </div>
                  <button onClick={() => { runNode(menu.nodeId); closeMenu(); }}>▶ 运行（先跑上游）</button>
                  <button onClick={() => { renameNode(menu.nodeId); closeMenu(); }}>✎ 重命名（R）</button>
                  <button className="danger"
                          onClick={() => { removeNode(menu.nodeId); closeMenu(); }}>✕ 删除节点</button>
                </>
              ) : menu.wireIdx !== undefined ? (
                <>
                  <div className="gv-menu-title">连线 #{menu.wireIdx}</div>
                  <button onClick={() => { removeEdge(menu.wireIdx); closeMenu(); }}>✕ 删除连线</button>
                </>
              ) : (
                <>
                  <button onClick={() => { setPalette({ x: menu.x, y: menu.y }); closeMenu(); }}>⊕ 添加节点…</button>
                  <button onClick={() => { runAll(); closeMenu(); }}>▶ 运行全部</button>
                  <button onClick={() => { fitView(); closeMenu(); }}>⛶ 适配视图</button>
                  <button onClick={() => { newGraph(); closeMenu(); }}>＋ 新建画布</button>
                </>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function NodeBadge({ node }) {
  const st = node.state;
  if (!st) return null;
  if (!st.ok) {
    if (st.blocked_by_review) {
      const isQC = /质检|质量|QC/.test(st.error || "");
      return <span className="gv-tag bad" title={st.error || ""}>{isQC ? "⛔ 质检拦截" : "⛔ 审核拦截"}</span>;
    }
    return <span className="gv-tag bad" title={st.error || ""}>失败</span>;
  }
  const out = st.outputs && Object.values(st.outputs)[0];
  const m = out && out.meta;
  const verdict = m && (m.verdict ?? m.passed);
  const note = m && m.note;
  const tip = [note, m && m.comments].filter(Boolean).join(" | ");
  if (verdict === "pass" || verdict === "ok" || verdict === true)
    return <span className="gv-tag pass" title={tip}>通过</span>;
  if (verdict === "reject" || verdict === false)
    return <span className="gv-tag reject" title={tip}>驳回</span>;
  if (verdict === "fix" || verdict === "unknown")
    return <span className="gv-tag pend" title={tip}>{verdict === "fix" ? "需修复" : "待质检"}</span>;
  if (verdict === "pending") return <span className="gv-tag pend" title={tip}>待审</span>;
  return <span className="gv-dot gv-dot-ok" title={tip || "执行成功"} />;
}

function ParamRow({ p, value, onChange }) {
  const s = p.schema || {};
  if (s.readonly) {
    return (
      <label className="gv-p">
        <span>{p.label}（锁定）</span>
        <em className="gv-ro">{String(s.value ?? value ?? "")}</em>
      </label>
    );
  }
  const t = s.type || "text";
  if (t === "select") {
    return (
      <label className="gv-p">
        <span>{p.label}</span>
        <select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
          {(s.options || []).map((o) => (
            <option key={o.value} value={o.value}>{o.label || o.value}</option>
          ))}
        </select>
      </label>
    );
  }
  if (t === "bool") {
    return (
      <label className="gv-p">
        <span>{p.label}</span>
        <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} />
      </label>
    );
  }
  if (t === "number") {
    return (
      <label className="gv-p">
        <span>{p.label}</span>
        <input type="number" min={s.min} max={s.max} step={s.step || 1}
               value={value ?? ""} onChange={(e) => onChange(e.target.value === "" ? "" : Number(e.target.value))} />
      </label>
    );
  }
  return (
    <label className="gv-p">
      <span>{p.label}</span>
      <textarea rows={3} placeholder={s.hint || ""} value={value ?? ""}
                onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

/** 多输出渲染：每个输出端口一行（文本/图片/视频/音频/审核/成品卡） */
function NodeOut({ node, gid, def }) {
  const st = node.state;
  const [src, setSrc] = useState(null);
  const outs = st && st.ok && st.outputs ? Object.entries(st.outputs) : [];
  const mediaKey = outs.map(([k, o]) => `${k}:${o && o.kind}`).join("|");
  const outLabels = {};
  for (const p of (def.outputs || [])) outLabels[p.name] = p.label;

  useEffect(() => {
    let alive = true, url = null;
    (async () => {
      const hasMedia = outs.some(([, o]) => o && (o.kind === "image" || o.kind === "video" || o.kind === "audio"));
      if (!hasMedia) return;
      try {
        const r = await fetch(`/api/graphs/${gid}/assets/${node.id}`, {
          headers: getApiKey() ? { "X-API-Key": getApiKey() } : {},
        });
        if (!alive) return;
        if (r.ok) { url = URL.createObjectURL(await r.blob()); setSrc(url); }
      } catch { /* 非鉴权模式直接降级 */ }
    })();
    return () => { alive = false; if (url) URL.revokeObjectURL(url); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [node.id, gid, mediaKey]);

  if (!st) return null;
  if (!st.ok) return <div className="gv-out bad" title={st.error || ""}>✗ 失败</div>;
  if (outs.length === 0) return null;

  return (
    <div className="gv-out">
      {outs.map(([port, o]) => {
        if (!o) return null;
        const label = outLabels[port] || port;
        if (o.kind === "image") return (
          <div key={port} className="gv-oport">
            <var>{label}</var>
            {src && <img src={src} alt={label} />}
          </div>);
        if (o.kind === "video") return (
          <div key={port} className="gv-oport">
            <var>{label}</var>
            {src && <video src={src} controls muted />}
          </div>);
        if (o.kind === "audio") return (
          <div key={port} className="gv-oport">
            <var>{label}</var>
            {src && <audio src={src} controls />}
          </div>);
        if (o.kind === "report") return (
          <div key={port} className="gv-oport">
            <var>{label}</var>
            <div className="gv-reportbox">
              {o.meta && o.meta.verdict
                ? `结论：${o.meta.verdict}${o.meta.stage ? `（${o.meta.stage}）` : ""}`
                : "报告已生成"}
              {o.meta && o.meta.comments ? ` — ${o.meta.comments}` : ""}
            </div>
          </div>);
        if (o.kind === "card") return (
          <div key={port} className="gv-oport">
            <var>{label}</var>
            <div className="gv-card">
              <div className="gv-card-title">{o.meta && (o.meta.title || "成品")}</div>
              {o.meta && o.meta.subtitle && <div className="gv-card-sub">{o.meta.subtitle}</div>}
              {(o.meta && o.meta.duration_sec) &&
                <div className="gv-card-row">时长 {o.meta.duration_sec}s</div>}
              {(o.meta && o.meta.tags && o.meta.tags.length) &&
                <div className="gv-card-tags">
                  {o.meta.tags.map((t) => <span key={t}>{t}</span>)}
                </div>}
              {src && <video src={src} controls muted />}
            </div>
          </div>);
        // text（剧本/分镜/提示词/审核建议…）
        return (
          <div key={port} className="gv-oport">
            {outs.length > 1 && <var>{label}</var>}
            <pre className="gv-textout">{String(o.value || "")}</pre>
          </div>);
      })}
    </div>
  );
}