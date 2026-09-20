// P5：路由外壳 —— `/` 项目列表 · `/projects/:pid` 看板 · `/login` 鉴权
// P6：新增 `/projects/:pid/timeline` 过程回放（桌面版看全程）
// P10：新增 `/graph` 节点画布（ComfyUI 式手操编排，连线=真实数据流）
import { HashRouter, Navigate, Route, Routes } from "react-router-dom";
import ProjectList from "./views/ProjectList";
import ProjectBoard from "./views/ProjectBoard";
import Timeline from "./views/Timeline";
import Login from "./views/Login";
import GraphView from "./views/GraphView";

export default function App() {
  return (
    <HashRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/" element={<ProjectList />} />
        <Route path="/projects/:pid" element={<ProjectBoard />} />
        <Route path="/projects/:pid/timeline" element={<Timeline />} />
        <Route path="/graph" element={<GraphView />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </HashRouter>
  );
}