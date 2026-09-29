// P5：路由外壳 —— `/` 项目列表 · `/projects/:pid` 看板 · `/login` 鉴权
// P6：新增 `/projects/:pid/timeline` 过程回放（桌面版看全程）
// P10：新增 `/graph` 节点画布（ComfyUI 式手操编排，连线=真实数据流）
// 参考复刻：新增 `/ref` 三栏向导（找参考 → 解构 → 注入画布）
import React from "react";
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import ProjectList from "./views/ProjectList";
import ProjectBoard from "./views/ProjectBoard";
import Timeline from "./views/Timeline";
import Login from "./views/Login";
import GraphView from "./views/GraphView";
import ReferenceStudio from "./views/ReferenceStudio";

// 轮68:错误边界——此前无任何边界,组件渲染抛错即 React 整树卸载、
// 静默白屏(无 console 无 window error),排错无从下手。现在至少把
// 错误栈打到界面上,崩溃可见、可报。
class ErrBoundary extends React.Component {
  constructor(p) { super(p); this.state = { err: null }; }
  static getDerivedStateFromError(err) { return { err }; }
  componentDidCatch(err, info) {
    console.error("[ErrBoundary]", err, info && info.componentStack);
    if (typeof window !== "undefined") {
      window.__E = window.__E || [];
      window.__E.push("BOUNDARY: " + String(err && err.message || err) + " | " +
        String(err && err.stack || "").split("\n").slice(0, 4).join(" <> "));
    }
  }
  render() {
    if (this.state.err) {
      return <pre style={{ color: "#f66", background: "#200", padding: 20,
                           whiteSpace: "pre-wrap", fontSize: 12 }}>
        {"前端渲染错误(轮68 ErrBoundary):\n" + String(this.state.err && this.state.err.stack || this.state.err).slice(0, 1500)}
      </pre>;
    }
    return this.props.children;
  }
}

export default function App() {
  return (
    <ErrBoundary>
    <HashRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<ProjectList />} />
        <Route path="/projects/:pid" element={<ProjectBoard />} />
        <Route path="/projects/:pid/timeline" element={<Timeline />} />
        <Route path="/graph" element={<GraphView />} />
        <Route path="/ref" element={<ReferenceStudio />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </HashRouter>
    </ErrBoundary>
  );
}