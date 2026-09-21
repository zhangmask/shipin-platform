// 节点画布 —— React Flow（@xyflow/react 12）重写版，交互对齐 ComfyUI：
//  - 左侧组件库按住拖拽进画布生成节点（onDrop 取世界坐标）
//  - 输出端口拖线到输入端口连线（同 kind 才可连，前后端双重校验）
//  - 画布平移 / 滚轮缩放 / 框选 / MiniMap / Controls / 网格背景
//  - 右键菜单：节点（运行/重命名/删除）与连线（删除）与空白（添加/运行全部/适配/新建）
//  - 快捷键：Delete 删选中、Ctrl+A 全选、Ctrl+Z/Y 撤销重做、Ctrl+C/V 复制粘贴、
//    F 适配视图、Esc 取消、R 重命名
//  - 双击空白打开搜索面板
//  - 运行全部（Queue Prompt 语义）：拓扑序逐节点执行，review/qc 门照常生效
//  - SSE 事件流：外部 AI 调平台 API 时节点状态实时推送
// 后端图协议不变（{id,type,x,y,params,state} + edges{from,from_port,to,to_port,order}），
// React Flow 仅是渲染层封装。
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  BackgroundVariant,
  Controls,
  MiniMap,
  Handle,
  Position,
  useNodesState,
  useEdgesState,
  useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { api, getApiKey } from "../lib";
import "./graph.css";

const NODE_W = 236; // 节点宽度，与 .gv-node 一致
const PORT_START = 46; // 端口区起始 y（头部下方）
const PORT_STEP = 26;  // 多端口垂直间距

function uid(prefix) {
  if (typeof crypto !== "undefined" && crypto.randomUUID) {
    return `${prefix}_${crypto.randomUUID().slice(0, 8)}`;
  }
  return `${prefix}_${Date.now().toString(36)}_${Math.floor(Math.random() * 1e4)}`;
}

// ---------------------------------------------------------------------------
// RF 状态 <-> 后端协议转换
// ---------------------------------------------------------------------------
const rfEdgeId = (e) => `${e.from}::${e.from_port}->${e.to}::${e.to_port}`;

const toRfNode = (n, def) => ({
  id: n.id,
  type: "gbox",
  position: { x: n.x, y: n.y },
  data: { node: n, def },
});

const toRfEdge = (e) => ({
  id: rfEdgeId(e),
  source: e.from,
  sourceHandle: e.from_port,
  target: e.to,
  targetHandle: e.to_port,
});

// 后端 rs 用（把 RF 状态折叠回协议：位置/连线/order）
function toProtoGraph(rfNodes, rfEdges, base) {
  const nodes = rfNodes.map((n) => {
    const old = base.nodes.find((x) => x.id === n.id) || {};
    return {
      id: n.id,
      type: old.type || n.data.node.type,
      x: Math.round(n.position.x),
      y: Math.round(n.position.y),
      params: old.params || n.data.node.params || {},
      ...(old.state ? { state: old.state } : {}),
      ...(old.title !== undefined ? { title: old.title } : {}),
    };
  });
  const edges = rfEdges.map((e, i) => ({
    from: e.source,
    from_port: e.sourceHandle || "",
    to: e.target,
    to_port: e.targetHandle || "",
    order: i,
  }));
  return { nodes, edges };
}

// ---------------------------------------------------------------------------
// 自定义节点 GBoxNode（保留原版头部/端口/参数/产物视觉）
// ---------------------------------------------------------------------------
function GBoxNode({ id, data, selected }) {
  const { node, def, gid, live, busy, editing, cb } = data;
  const isRun = busy === node.id || live[node.id];
  const ins = def.inputs || [];
  const outs = def.outputs || [];
  const d = def;

  return (
    <div className={"gv-node" + (selected ? " sel" : "") + (isRun ? " run" : "")}>
      {/* 输入端口（左缘） */}
      {ins.map((p, pi) => (
        <Handle key={`in_${p.name}`} type="target" position={Position.Left}
                id={p.name}
                className="gv-handle in"
                style={{ top: `${PORT_START + pi * PORT_STEP}px`, left: 0,
                         background: d.color }}
                title={`${p.label}${p.required ? "（必连）" : ""}`}>
          <em>{p.label}</em>
        </Handle>
      ))}
      {/* 输出端口（右缘） */}
      {outs.map((p, oi) => (
        <Handle key={`out_${p.name}`} type="source" position={Position.Right}
                id={p.name}
                className="gv-handle out"
                style={{ top: `${PORT_START + oi * PORT_STEP}px`, right: 0,
                         background: d.color }}
                title={p.label}>
          <em>{p.label}</em>
        </Handle>
      ))}

      <div className="gv-node-head" style={{ background: d.color }}>
        {editing === node.id ? (
          <input className="gv-title-edit" defaultValue={node.title || d.label}
                 autoFocus onFocus={(e) => e.target.select()}
                 onClick={(e) => e.stopPropagation()}
                 onBlur={(e) => cb.commitRename(node.id, e.target.value)}
                 onKeyDown={(e) => {
                   if (e.key === "Enter") cb.commitRename(node.id, e.target.value);
                   if (e.key === "Escape") cb.cancelRename();
                 }} />
        ) : (
          <span className="gv-node-title" title="双击重命名"
                onDoubleClick={(e) => { e.stopPropagation(); cb.rename(node.id); }}>
            {node.title || d.label}
          </span>
        )}
        <span className="gv-node-tools">
          <NodeBadge node={node} />
          {isRun ? <span className="gv-spin" />
            : <button className="gv-run" title="运行（先跑上游）"
                      onClick={(e) => { e.stopPropagation(); cb.run(node.id); }}>▶</button>}
          <button className="gv-del" title="删除节点"
                  onClick={(e) => { e.stopPropagation(); cb.remove(node.id); }}>×</button>
        </span>
      </div>

<div className="gv-params">
        {(d.params || []).map((p) => (
          <ParamRow key={p.key} p={p}
                    value={node.params?.[p.key]}
                    onChange={(v) => cb.param(node.id, p.key, v)} />
        ))}
      </div>
      <NodeOut node={node} gid={g} def={def} />
    </div>
  );
}

const nodeTypes = { gbox: GBoxNode };

// ---------------------------------------------------------------------------
// 画布主体
// ---------------------------------------------------------------------------
function Board() {
  const nav = useNavigate();
  const [sp] = useSearchParams();
  const [defs, setDefs] = useState(null);
  const [graphs, setGraphs] = useState([]);
  const [cur, setCur] = useState(null);          // 后端图 JSON（真值）
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(null);        // "node_id" | "__all__" | null
  const [live, setLive] = useState({});         // SSE 运行中 {node_id: true}
  const [menu, setMenu] = useState(null);       // {x, y, nodeId?|wireId?}
  const [palette, setPalette] = useState(null); // 双击空白搜索面板 {x,y}
  const [palQ, setPalQ] = useState("");
  const [palIdx, setPalIdx] = useState(0);
  const [editing, setEditing] = useState(null); // 正在重命名的节点 id
  const [clip, setClip] = useState(null);       // 复制剪贴板 {nodes, edges}
  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const { screenToFlowPosition, fitView } = useReactFlow();
  const saveTimer = useRef(null);
  const reloadTimer = useRef(null);
  const palInput = useRef(null);
  const histRef = useRef([]);   // undo 栈（{nodes, edges} 快照）
  const histIdx = useRef(-1);
  const selRef = useRef(null);  // 最近一次单选节点 id（供 R 重命名）

  // ---- 数据加载 ----
  const loadDefs = useCallback(async () => {
    setDefs((await api("/api/graphs/kit/definitions")).nodes);
  }, []);
  const refreshList = useCallback(async () => {
    setGraphs((await api("/api/graphs")).graphs || []);
  }, []);
  const loadGraph = useCallback(async (gid) => {
    const r = await api(`/api/graphs/${gid}`);
    setCur(r.graph);
    if (r.errors && r.errors.length) setErr(`画布校验：${r.errors.join("; ")}`);
    else setErr(null);
  }, []);
  useEffect(() => { loadDefs(); refreshList(); }, [loadDefs, refreshList]);

  // URL ?g=<gid>（参考复刻注入跳转）→ 自动切换画布
  const urlGid = sp.get("g");
  useEffect(() => {
    if (urlGid && urlGid !== (cur && cur.id)) loadGraph(urlGid);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [urlGid]);

  // ---- 保存 ----
  const scheduleSave = useCallback((proto) => {
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(async () => {
      if (!proto || !proto.id) return;
      try {
        const r = await api(`/api/graphs/${proto.id}`, {
          method: "PUT",
          body: JSON.stringify({ name: proto.name, nodes: proto.nodes, edges: proto.edges }),
        });
        if (r && r.errors && r.errors.length) setErr(r.errors.join("; "));
      } catch (e) { setErr(`保存失败：${e.message}`); }
    }, 300);
  }, []);

  // 从后端图重建 RF 状态（图切换 / SSE 重载；保留选择态）
  const rebuild = useCallback((g) => {
    if (!g) { setNodes([]); setEdges([]); return; }
    setNodes((prev) => {
      const keep = new Map(prev.map((n) => [n.id, n]));
      return g.nodes
        .filter((n) => defs && defs[n.type])
        .map((n) => {
          const old = keep.get(n.id);
          return { ...toRfNode(n, defs[n.type]),
                   selected: !!(old && old.selected) };
        });
    });
    setEdges(g.edges.map(toRfEdge));
  }, [defs, setNodes, setEdges]);
  // 仅图 id 变化时重建（拖动位置同步不触发，避免拖拽抖动）
  useEffect(() => { rebuild(cur); }, [cur && cur.id]);
  // eslint-disable-next-line react-hooks/exhaustive-deps

  // ---- 变更核心：改 cur（真值）+ 防抖保存 + 记录历史 ----
  /** 注意：节点的 params/title/state 存储在 cur；坐标/连线以 RF 为准，
      保存时合并。所有增删改统一走 mutate(protocolGraph)。 */
  const mutate = useCallback((fn, opts = {}) => {
    if (!cur) return;
    const g = JSON.parse(JSON.stringify(cur));
    if (!opts.noHistory) {
      histRef.current = histRef.current.slice(0, histIdx.current + 1);
      histRef.current.push({ nodes: g.nodes, edges: g.edges });
      if (histRef.current.length > 80) histRef.current.shift();
      histIdx.current = histRef.current.length - 1;
    }
    fn(g);
    setCur(g);
    scheduleSave(g);
    // 同步 RF 结构（位置变化由 onNodesChange 自行增量处理）
    if (opts.syncRf !== false) {
      setNodes(g.nodes
        .filter((n) => defs && defs[n.type])
        .map((n) => toRfNode(n, defs[n.type])));
      setEdges(g.edges.map(toRfEdge));
    }
  }, [cur, defs, scheduleSave, setNodes, setEdges]);

  // ---- undo / redo ----
  const applyHistory = useCallback((dir) => {
    const i = histIdx.current + dir;
    if (i < 0 || i >= histRef.current.length) return;
    histIdx.current = i;
    const snap = histRef.current[i];
    setCur((g) => {
      if (!g) return g;
      // 只恢复 nodes/edges，保留 id/name
      const ng = JSON.parse(JSON.stringify(g));
      ng.nodes = snap.nodes.map((n) => ({ ...n }));
      ng.edges = snap.edges.map((e) => ({ ...e }));
      setNodes(ng.nodes
        .filter((n) => defs && defs[n.type])
        .map((n) => toRfNode(n, defs[n.type])));
      setEdges(ng.edges.map(toRfEdge));
      scheduleSave(ng);
      return ng;
    });
  }, [defs, scheduleSave, setNodes, setEdges]);

  const undo = useCallback(() => applyHistory(-1), [applyHistory]);
  const redo = useCallback(() => applyHistory(1), [applyHistory]);

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
          setTimeout(() => { if (cur) loadGraph(cur.id); }, 200);
        }
        return;
      }
      if (ev.type === "run" || ev.type === "run_all") {
        if (ev.node_id) setLive((p) => ({ ...p, [ev.node_id]: true }));
        return;
      }
      if (ev.type === "node" && ev.op) setTimeout(() => { if (cur) loadGraph(cur.id); }, 200);
      if (ev.type === "changed") setTimeout(() => { if (cur) loadGraph(cur.id); }, 200);
    };
    return () => es.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cur && cur.id]);

  // ---- 键盘 ----
  useEffect(() => {
    const onKey = (e) => {
      const tag = (e.target && e.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      const mod = e.ctrlKey || e.metaKey;
      const k = e.key.toLowerCase();
      if (e.key === "Escape") {
        setMenu(null); setPalette(null); setEditing(null);
        return;
      }
      if (mod && k === "z") {
        e.preventDefault();
        if (e.shiftKey) redo(); else undo();
        return;
      }
      if (mod && k === "y") { e.preventDefault(); redo(); return; }
      if (mod && k === "a") {
        if (cur) {
          e.preventDefault();
          setNodes((ns) => ns.map((n) => ({ ...n, selected: true })));
        }
        return;
      }
      if (mod && k === "c") {
        if (!cur) return;
        const selN = nodes.filter((n) => n.selected);
        if (!selN.length) return;
        e.preventDefault();
        const ids = new Set(selN.map((n) => n.id));
        setClip({
          nodes: selN.map((n) => ({
            id: n.id,
            type: n.data.node.type,
            x: n.position.x, y: n.position.y,
            params: n.data.node.params || {},
            title: n.data.node.title,
          })),
          // 复制选中组内连线（粘贴时按新 id 重连）
          edges: edges.filter((e) => ids.has(e.source) && ids.has(e.target))
                      .map((e) => ({
                        from: e.source, from_port: e.sourceHandle,
                        to: e.target, to_port: e.targetHandle,
                      })),
        });
        return;
      }
      if (mod && k === "v") {
        if (cur && clip && clip.nodes && clip.nodes.length) {
          e.preventDefault();
          mutate((g) => {
            const map = {};
            clip.nodes.forEach((c) => {
              const nid = uid(c.type);
              map[c.id] = nid;
              g.nodes.push({
                id: nid, type: c.type,
                x: Math.round(c.x + 40), y: Math.round(c.y + 40),
                params: { ...c.params },
                ...(c.title ? { title: c.title } : {}),
              });
            });
            clip.edges.forEach((e2) => {
              const f = map[e2.from], t = map[e2.to];
              if (f && t) g.edges.push({
                from: f, from_port: e2.from_port,
                to: t, to_port: e2.to_port,
                order: g.edges.length,
              });
            });
          });
        }
        return;
      }
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        deleteSelected();
        return;
      }
      if (k === "f") { fitView({ padding: 0.15, duration: 200 }); return; }
      if ((e.key === "r" || e.key === "R") && cur) {
        const selId = selRef.current;
        if (selId && nodes.some((n) => n.id === selId)) setEditing(selId);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cur, nodes, edges, clip, undo, redo, fitView]);

  // ---- 删除选中 ----
  const deleteSelected = () => {
    const delN = new Set(nodes.filter((n) => n.selected).map((n) => n.id));
    const delE = edges.filter((e) => e.selected).map((e) => e.id);
    if (!delN.size && !delE.length) return;
    mutate((g) => {
      g.nodes = g.nodes.filter((n) => !delN.has(n.id));
      g.edges = g.edges.filter((e) =>
        !delE.has(rfEdgeId(e)) && !delN.has(e.from) && !delN.has(e.to));
    });
  };

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
      const res = (r.results && r.results.nodes) || {};
      const fails = Object.values(res).filter((x) => !x.ok);
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

  // ---- 节点增删改 ----
  const addNodeAt = (type, wx, wy) => {
    if (!cur || !defs) return;
    const d = defs[type];
    const params = {};
    for (const p of (d.params || [])) {
      if (p.schema && typeof p.schema.value !== "undefined") params[p.key] = p.schema.value;
    }
    const x = Math.round(wx), y = Math.round(wy);
    const nid = uid(type);
    mutate((g) => g.nodes.push({ id: nid, type, x, y, params }));
    selRef.current = nid;
  };
  const newNode = (type) => {
    if (!cur) { setErr("请先选择或新建画布"); return; }
    const n = cur.nodes.length;
    addNodeAt(type, 90 + (n % 4) * 90 + 20 * (n % 2), 60 + (n % 5) * 58);
  };
  const removeNode = (id) => {
    mutate((g) => {
      g.nodes = g.nodes.filter((n) => n.id !== id);
      g.edges = g.edges.filter((e) => e.from !== id && e.to !== id);
    });
  };
  const renameNode = (id) => { selRef.current = id; setEditing(id); };
  const cancelRename = () => setEditing(null);
  const commitRename = (id, title) => {
    mutate((g) => {
      const m = g.nodes.find((x) => x.id === id);
      if (!m) return;
      if (title && title.trim()) m.title = title.trim();
      else delete m.title;
    });
    setEditing(null);
  };
  const updateParam = (nodeId, key, val) => {
    mutate((g) => {
      const n = g.nodes.find((x) => x.id === nodeId);
      if (n) n.params[key] = val;
    });
  };

  // ---- 连线 ----
  const isValidConnection = useCallback((conn) => {
    if (!defs || !cur) return false;
    if (conn.source === conn.target) return false;
    const s = cur.nodes.find((n) => n.id === conn.source);
    const t = cur.nodes.find((n) => n.id === conn.target);
    if (!s || !t) return false;
    const sd = defs[s.type], td = defs[t.type];
    if (!sd || !td) return false;
    const sp = sd.outputs.find((p) => p.name === conn.sourceHandle);
    const tp = td.inputs.find((p) => p.name === conn.targetHandle);
    if (!sp || !tp) return false;
    if (sp.kind !== tp.kind) return false;
    // 同一对端口已连？不重复
    if (cur.edges.some((e) =>
      e.from === conn.source && e.from_port === conn.sourceHandle &&
      e.to === conn.target && e.to_port === conn.targetHandle)) return false;
    return true;
  }, [defs, cur]);

  const onConnect = useCallback((conn) => {
    if (!isValidConnection(conn)) return;
    mutate((g) => {
      g.edges.push({
        from: conn.source, from_port: conn.sourceHandle || "",
        to: conn.target, to_port: conn.targetHandle || "",
        order: g.edges.length,
      });
    });
  }, [isValidConnection, mutate]);

  // RF 状态变更 → 落回 cur（位置/尺寸拖拽有关，防抖保存）
  const onNodesChanged = useCallback((changes) => {
    onNodesChange(changes);
    // 位置变了 → 拦截拖动结束，把 RF 位置写回 cur 协议
    const moved = changes.some((c) => c.type === "position" && c.position != null);
    if (!moved || !cur) return;
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => {
      setCur((g) => {
        if (!g) return g;
        const nx = g.nodes.slice().map((n) => ({ ...n }));
        let dirty = false;
        nodes.forEach((rf) => {
          const m = nx.find((x) => x.id === rf.id);
          if (m && (m.x !== Math.round(rf.position.x) || m.y !== Math.round(rf.position.y))) {
            m.x = Math.round(rf.position.x); m.y = Math.round(rf.position.y);
            dirty = true;
          }
        });
        if (!dirty) return g;
        const ng = { ...g, nodes: nx };
        scheduleSave(ng);
        return ng;
      });
    }, 250);
  }, [onNodesChange, cur, nodes, scheduleSave]);

  const onNodesDelete = useCallback((deleted) => {
    if (!cur || !deleted.length) return;
    const ids = new Set(deleted.map((d) => d.id));
    mutate((g) => {
      g.nodes = g.nodes.filter((n) => !ids.has(n.id));
      g.edges = g.edges.filter((e) => !ids.has(e.from) && !ids.has(e.to));
    });
  }, [cur, mutate]);
  const onEdgesDelete = useCallback((deleted) => {
    if (!cur || !deleted.length) return;
    const del = new Set(deleted.map((d) => d.id));
    mutate((g) => {
      g.edges = g.edges.filter((e) => !del.has(rfEdgeId(e)));
    });
  }, [cur, mutate]);

  // ---- 画布交互 ----
  const onDrop = useCallback((event) => {
    event.preventDefault();
    const type = event.dataTransfer.getData("application/shipin-node");
    if (!type || !cur) return;
    const p = screenToFlowPosition({ x: event.clientX, y: event.clientY });
    addNodeAt(type, p.x, p.y);
  }, [screenToFlowPosition, cur, addNodeAt]);
  const onDragOver = useCallback((event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, []);

  // ---- 右键菜单 ----
  const closeMenu = () => setMenu(null);
  const nodeMenu = (e) => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY, nodeId: e.node ? e.node.id : null }); };
  const edgeMenu = (e) => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY, edgeId: e.edge ? e.edge.id : null }); };
  const paneMenu = (e) => { e.preventDefault(); setMenu({ x: e.clientX, y: e.clientY }); };

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
    const p = screenToFlowPosition({ x: palette.x, y: palette.y });
    addNodeAt(k, p.x, p.y);
    setPalette(null);
  };
  const openPaletteAt = (e) => {
    const el = e.target;
    // 不在节点上才打开
    if (el && el.closest && el.closest(".gv-node")) return;
    setPalette({ x: e.clientX, y: e.clientY });
    setPalQ(""); setPalIdx(0);
  };

  // ---- 新建/删除图 ----
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
    setCur(null); setNodes([]); setEdges([]);
    await refreshList();
  };

  // GBoxNode 高频 callback（组件级共享，避免每节点重建）
  const cb = useMemo(() => ({
    run: runNode, remove: removeNode, rename: renameNode,
    commitRename, cancelRename, param: updateParam,
  }), [runNode, removeNode, renameNode, updateParam, commitRename, cancelRename]);

  // ---- 渲染 ----
  return (
    <div className="gv-root" onMouseDown={menu ? closeMenu : undefined}>
      <div className="gv-topbar">
        <b>节点画布</b>
        <select value={cur ? cur.id : ""}
                onChange={(e) => { const v = e.target.value; if (v) { setCur(null); loadGraph(v); } }}>
          <option value="">选择画布…</option>
          {graphs.map((g) => <option key={g.id} value={g.id}>{g.name}</option>)}
        </select>
        <button onClick={newGraph}>＋ 新建画布</button>
        <button onClick={deleteGraph} disabled={!cur}>删除画布</button>
        <button onClick={() => setPalette({ x: 420, y: 120 })} disabled={!cur}>⊕ 添加节点</button>
        <button onClick={() => nav("/ref")} title="从 B站/抖音/YouTube 找参考视频，反推剧本分镜提示词，一键灌入画布"
                style={{ background: "#3f5bdb" }}>🎬 参考复刻</button>
        <button onClick={runAll} disabled={!cur || !!busy}
                title="拓扑序运行全部节点（审核/质检门照常生效）">▶ 运行全部</button>
        <button onClick={() => fitView({ padding: 0.15, duration: 200 })} disabled={!cur}
                title="适配视图（F）">⛶ 适配</button>
        <button onClick={undo} disabled={histIdx.current <= 0}>↶ 撤销</button>
        <button onClick={redo} disabled={histIdx.current >= histRef.current.length - 1}>↷ 重做</button>
        <span className="gv-tip">
          拖组件库入画布 · 拖空白平移 · Ctrl+拖框选 · F 适配 · Ctrl+Z/Y 撤销 · Ctrl+A 全选 · Delete 删除 · R 改名
        </span>
        {err && <span className="gv-err">{err}</span>}
      </div>

      <div className="gv-main">
        <aside className="gv-libs">
          <div className="gv-libs-title">组件库（拖到画布）</div>
          {defs && Object.keys(defs).map((k) => {
            const d = defs[k];
            return (
              <button key={k} className="gv-lib" style={{ borderColor: d.color }}
                      draggable onDragStart={(e) => {
                        e.dataTransfer.setData("application/shipin-node", k);
                        e.dataTransfer.effectAllowed = "copy";
                      }}
                      title={d.hint || ""} onClick={() => newNode(k)}>
                <span className="gv-libdot" style={{ background: d.color }} />
                {d.label}
                <small>{d.category}</small>
              </button>
            );
          })}
          {!defs && <span className="gv-libs-load">组件库加载中…</span>}
        </aside>

        <div className="gv-canvas">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            onNodesChange={onNodesChanged}
            onEdgesChange={onEdgesChange}
            onConnect={onConnect}
            onNodesDelete={onNodesDelete}
            onEdgesDelete={onEdgesDelete}
            isValidConnection={isValidConnection}
            onNodeContextMenu={nodeMenu}
            onEdgeContextMenu={edgeMenu}
            onPaneContextMenu={paneMenu}
            onDrop={onDrop}
            onDragOver={onDragOver}
            onPaneDoubleClick={openPaletteAt}
            nodeTypes={nodeTypes}
            deleteKeyCode={null}
            selectionKeyCode={["ShiftLeft", "ShiftRight"]}
            multiSelectionKeyCode={["ControlLeft", "ControlRight"]}
            selectionOnDrag={false}
            panOnDrag={[0, 1]}
            panOnScroll={false}
            zoomOnScroll
            minZoom={0.1}
            maxZoom={3}
            fitView
            fitViewOptions={{ padding: 0.2 }}
            defaultEdgeOptions={{
              type: "smoothstep",
              style: { stroke: "#5b6b9e", strokeWidth: 2.5 },
            }}
            proOptions={{ hideAttribution: true }}
          >
            <Background variant={BackgroundVariant.Dots} gap={26} size={1.4}
                        color="#23283866" bgColor="#0f111a" />
            <MiniMap nodeColor={(n) => n.data?.def?.color || "#64748b"}
                     bgColor="#0d0f16" maskColor="rgba(45, 52, 90, 0.35)"
                     pannable style={{ width: 140, height: 90 }} />
            <Controls position="bottom-right" showInteractive={false} />
          </ReactFlow>

          {!cur && (
            <div className="gv-empty">
              点左上角「＋ 新建画布」或选择已有画布。<br />
              节点 = 真实服务端执行：剧本 → 分镜 → 首尾帧提示词 → 生图/生视频 →
              QC → 审核 → 拼接出片 → 成品卡片；连线 = 产物真实流入下游。
            </div>
          )}

          {palette && (
            <div className="gv-palette" style={{ left: palette.x - 320, top: palette.y }}
                 onMouseDown={(e) => e.stopPropagation()}>
              <input ref={palInput} autoFocus placeholder="搜索节点…（↑↓ 选择，Enter 添加）"
                     value={palQ}
                     onChange={(e) => { setPalQ(e.target.value); setPalIdx(0); }}
                     onKeyDown={(e) => {
                       if (e.key === "Escape") setPalette(null);
                       else if (e.key === "ArrowDown") { e.preventDefault(); setPalIdx((i2) => Math.min(i2 + 1, palList.length - 1)); }
                       else if (e.key === "ArrowUp") { e.preventDefault(); setPalIdx((i2) => Math.max(i2 - 1, 0)); }
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
                  <button onClick={() => {
                    const id = menu.nodeId;
                    const copy = cur.nodes.find((n) => n.id === id);
                    if (copy) {
                      mutate((g) => {
                        g.nodes.push({
                          id: uid(copy.type), type: copy.type,
                          x: copy.x + 60, y: copy.y + 60,
                          params: { ...copy.params },
                          title: copy.title,
                        });
                      });
                    }
                    closeMenu();
                  }}>⧉ 复制节点</button>
                  <button className="danger"
                          onClick={() => { removeNode(menu.nodeId); closeMenu(); }}>✕ 删除节点</button>
                </>
              ) : menu.edgeId ? (
                <>
                  <div className="gv-menu-title">连线</div>
                  <button onClick={() => {
                    const id = menu.edgeId;
                    const e = edges.find((x) => x.id === id);
                    if (e && cur) {
                      mutate((g) => {
                        g.edges = g.edges.filter((x) => rfEdgeId(x) !== id);
                      });
                    }
                    closeMenu();
                  }} className="danger">✕ 删除连线</button>
                </>
              ) : (
                <>
                  <button onClick={() => { setPalette({ x: menu.x, y: menu.y }); closeMenu(); }}>⊕ 添加节点…</button>
                  <button onClick={() => { runAll(); closeMenu(); }}>▶ 运行全部</button>
                  <button onClick={() => { fitView({ padding: 0.15, duration: 200 }); closeMenu(); }}>⛶ 适配视图</button>
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

// React Flow hook 需在 Provider 内使用
export default function GraphView() {
  return (
    <ReactFlowProvider>
      <Board />
    </ReactFlowProvider>
  );
}

// ---------------------------------------------------------------------------
// 节点状态徽标（原实现保留）
// ---------------------------------------------------------------------------
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

// ---------------------------------------------------------------------------
// 参数行（readonly/select/bool/number/text）
// ---------------------------------------------------------------------------
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

/** 多输出渲染：端口行 + 产物预览（图片/视频/音频/报告卡片/文本） */
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
        // 文本（剧本/分镜/提示词/审核建议…）
        return (
          <div key={port} className="gv-oport">
            {outs.length > 1 && <var>{label}</var>}
            <pre className="gv-textout">{String(o.value || "")}</pre>
          </div>);
      })}
    </div>
  );
}