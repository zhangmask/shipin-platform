# shipin-platform 部署实测报告

> 机器：远程 NVIDIA DGX Spark（asus_gx10@61.172.235.130:6023，GB10 / ~130GB 统一内存）
> 项目：https://github.com/zhangmask/shipin-platform
> 实测日期：2026-09-23
> 交付物：一支 5 镜 20.6s 品牌 TVC（瑞幸生椰拿铁），全部媒体由 DGX 本地模型生成
>
> 本文件夹内容：
> - `README.md`（本文件）——总览与结论
> - `problems.md`——发现的问题清单（按严重度）
> - `fixes.md`——本次已修复的 bug 与改动明细
> - `suggestions.md`——产品与工程建议
> - `verification.md`——数据/历史记录/节点画布的验证结果
> - `artifacts.md`——可复现的实验参数与产物清单

---

## 一、总体结论

**项目可以在 DGX Spark 上完整跑通「一句话 → 成片」全流程**，媒体侧（图/视频/配音/音乐）
全部走本地模型，LLM 侧走 agnes-3.0-flash API。过程中发现并修复了 **10 个 bug**
（6 个代码缺陷、2 个模板/数据形状缺陷、2 个配置/工程缺陷；其中 4 个是只在本地后端
暴露、官方云端演示路径永远触发的），均在本报告 `fixes.md` 列明。

最终成片（`final.mp4`，22.3s，720×1280 竖屏，-14.34 LUFS）包含：
H3 图生视频镜头链、逐镜 QC、VibeVoice 旁白**与角色台词双语音轨**
（含静音裁剪与变速适配）、**本地 MiniMax Music 3 生成的 BGM（旁白闪避混音）**、
切点音效、转场、调色、字幕烧录与响度归一。全部确定性质量门通过
（保时长/字幕/响度/时间线/旁白声轨/品牌可见）；终审 VLM 内容审查对 3 个镜头
仍有提示（H3 生成随机性，详见 `verification.md` 第 4 节），按 fail-closed 原则
未标记 RELEASED，成片以 candidate 形态保留、可直接播放评估。

## 二、部署形态（一句话版）

```
用户/公网 7023 ──► shipin-platform (uvicorn :7000, SPA /ui/)
                      │  LLM 请求
                      ▼
                agnes-3.0-flash（经本机反向隧道 8790 代理到 apihub.agnes-ai.com）
                      │  媒体请求（全部本地）
                      ▼
                h3api (:9000) ──► ComfyUI 主实例 (:8188, MiniMax-H3 视频)
                               └─► ComfyUI 小实例 (:8189, zimage 图 / VibeVoice 配音 / Music3 音乐)
```

关键取舍：节点所在机房无法直连 agnes API（Cloudflare 阻断）与 GitHub/HuggingFace，
分别用「本机反向隧道代理」和「ModelScope 镜像 + 单文件同步」解决。

## 三、最值得知道的五件事

1. **本地生成质量可用**：H3 三镜连续出片，zimage 首帧 720×1280 一次过审，
   Music3 的 BGM 直接可用；VibeVoice 短句配音是最大的不稳定源（见 P-01）。
2. **平台的"确定性门"设计是真的有效**：字幕溢出、音画时间线劈叉、旁白越窗、
   素材哈希狸猫换太子都被硬门拦下并给出可操作原因，没有让烂片流到下一阶段。
3. **数据血缘记录完整**：每镜的输入指纹/clip sha256/审查结论/版本史/成本账目
   全部落盘，`manifest.json` 就是"这片子怎么来的"答案（见 `verification.md`）。
4. **节点画布可用**：React Flow 12 重写的类 ComfyUI 画布已部署，本次为 TVC 项目
   建了一支 24 节点/22 连线的复刻图，可在 `/ui/#/graph` 打开并手改重跑。
5. **本地后端仍有 6 处代码级水土不服**（见 `fixes.md`），都是 agnes 云端协议下
   不会触发、换本地后端才暴露的路径，说明项目的"本地后端"覆盖并不完整。

## 四、复现入口

| 用途 | 入口 |
|---|---|
| 平台 UI | http://61.172.235.130:7023/ui/ |
| 图协议 API | `GET /api/graphs/kit/definitions`（节点类型清单） |
| 本项目成片 | 节点 `~/shipin-platform/data/projects/tvc_luckin_01/final.mp4` |
| 部署文档 | 节点 `~/DEPLOYMENT.md` |
| 成本账目 | `data/projects/tvc_luckin_01/cost.json` |
