# 验证结果：数据记录 / 历史 / 节点画布

验证对象：节点 `~/shipin-platform/data/projects/tvc_luckin_01/`（本次实测项目）。

---

## 1. 生成数据记录 —— 完整（PASS）

`data/projects/tvc_luckin_01/` 目录结构：

```
brief.json / brief_review.json            创意简报 + 审片报告
script.json / script_review.json          剧本（5 镜）+ 审片报告
storyboard.json / storyboard_review.json  分镜（每镜 11 字段）+ 审片报告
image_prompt.json / image_prompt_review.json   派生提示词 + 审片报告
video_prompt.json / video_prompt_review.json   派生提示词 + 审片报告
manifest.json                             ★ 每镜血缘（见下）
cost.json                                 ★ 成本账目（46 条，seq/ts/kind/model/units/usd/note）
versions.json + versions/<stage>/v*.json  ★ 分阶段版本史（内容哈希 + 时间 + 调用方）
shots_review.json / stitch_parts.json / stitch_result.json / subtitle_check.json
S01..S05.jpg / _clip.mp4 / _canvas.mp4    每镜首帧、原始 clip、画幅版 clip
parts/p00..p04.mp4                        入拼片段（含 source=clip/master/freeze 标注）
stitched.mp4 → graded.mp4 → subtitled.mp4 → final.mp4 (+ final.candidate.mp4)
subs.srt / preview_frames/
```

### manifest.json（每镜血缘，节选字段）

```json
"S01": {
  "first_frame": ".../S01.jpg",
  "last_frame":  ".../S02.jpg",            ← 链式首尾帧策略的落点
  "boundary":    "cut",                    ← 边界类型驱动转场选型
  "keyframe_review": "ok", "qc": "ok",
  "clip": ".../S01_canvas.mp4",
  "clip_input_sha":  "a9888bc846c4ec51",   ← 输入指纹（提示词+首末帧+时长）
  "clip_shot_sha":   "dd201cbca2f3543c",
  "clip_sha256": "ee277719...c6b8a",       ← 内容哈希（assemble 时三方对账）
  "tts": ".../S01_02b56f01.mp3",
  "tts_text_sha": "66e58e876d0e255d"
}
```

**结论**：数据记录设计是本项目最扎实的部分之一——每镜"用什么输入、出了什么、
审过没有、内容指纹"全部可追溯；`clip_sha256` 在 assemble 时与 manifest 对账，
素材被替换会硬拦（"素材内容与 generate 阶段验收时不一致……禁止拼接"）。
**唯一账目缺陷见 P-06（本地生成记了云端价）。**

### 版本史实测

`versions.json`：brief 2 版、script 3 版（审片-修复循环的产物）、storyboard 3 版、
image_prompt/video_prompt 各 1 版；每版带 ISO 时间、sha256、caller、字节数。
文件级副本在 `versions/<stage>/v*.json`，可逐版 diff。

---

## 2. 历史/事件流 —— 有，但入口深 (PASS with note)

- `data/stage_store.db`（SQLite）：阶段状态机（brief/script/storyboard/... 各自
  status + 审片记录 + 人工确认闸门 `record_confirmation`）；
- `data/audit_log.db`：审计流水；
- 阶段事件（phase_started / phase_finished / assemble_failed）由
  `store.record_event` 落库，generate/assemble 的每次起停都有记录；
- `stage_runs` 支撑异步任务轮询（`POST ...?async=true` → 202 → `GET /api/tasks/{id}`）。

**note**：事件与阶段状态只进 SQLite，没有面向用户的"操作历史"页面/端点
（对比 versions.json 是面向文件的）。建议加一个 `GET /api/projects/{id}/timeline`
聚合事件+版本+成本，UI 直接渲染（对应 suggestions.md S-02 的同一处缺口）。

---

## 3. 节点画布（类 ComfyUI）—— 可用 (PASS)

| 检查项 | 结果 |
|---|---|
| 前端构建产物 | `web/dist`（React + @xyflow/react 12），节点部署副本与本地同指纹 |
| 访问入口 | `http://61.172.235.130:7023/ui/`（SPA，`/ui/<路由>` 含画布页） |
| 后端图协议 | `/api/graphs`（CRUD / run / run-all / nodes / params / events SSE / assets / kit） |
| 节点 kit | text / script / storyboard / frame_prompts / video_prompt / image_gen / video_gen / tts / qc / assemble，含端口类型与参数 schema |
| 校验 | 建图保存返回 `{"ok": true, "errors": []}`；非法图 422 `INVALID_GRAPH` |
| 实测建图 | 为本 TVC 项目建复刻图 `g-20260923224953-8ae092ec`：**24 节点 / 22 连线 / 0 校验错误** |

### 为这支 TVC 建的复刻图（"最终版是怎么做的"直接答案）

节点全部带**真实参数**：每镜的 image_gen 节点内是当次实际使用的英文提示词与
720×1280 画幅；video_gen 节点内是实际视频提示词与 3s 时长；tts 节点内是当句旁白；
qc 节点是 `expected_duration=3 / max_internal_cuts=0`；assemble 节点是
`fps=24 / color_grade=true / burn_audio=true`。连线反映真实依赖：

```
brief(text) → script → storyboard ─┬→ img_S0x → vid_S0x ─┬→ qc_S0x
                                   │                      └→ assemble(按连线顺序)
                                   └→ …每镜一列
```

用户可以打开画布看到每条边是什么数据、改任一节点的 prompt/参数后单独重跑
（`POST /api/graphs/{gid}/run {node_id}`）或整体 `run-all`（拓扑序逐节点执行，
review/qc 门照常生效）。画布交互（拖拽建点、同类型端口连线、右键运行/重命名/删除、
Delete/Ctrl+Z/Ctrl+C/V、MiniMap、SSE 实时状态）均在前端实现层。

**注意**：节点 kit 的模型下拉只列 agnes 云端模型名；本地部署时在节点标题里
标注了真实引擎（zimage / MiniMax-H3 / VibeVoice / Music3），参数值如实填写，
不影响保存与重跑（重跑走 `SHIPIN_MEDIA_BACKEND=local`）。

---

## 4. 终审复审（storyboard 修复迭代后）

> 本节在最终一轮 generate → assemble 完成后回填。

**最终态（第 14 轮 generate + 第 6 轮 assemble，2026-09-24 16:18）**：

| 门 | 结果 |
|---|---|
| stitch 保时长 | 21.75s ≈ 预期 21.77s，`boundary_preserved: true` |
| 字幕验收（§10.6） | 0 critical / 0 warning |
| 响度归一 | **-14.34 LUFS**（目标 -14） |
| 时间线门 / 旁白声轨门 | 双双 ok |
| 确定性层（内切/黑场/尖峰/音画） | internal_cuts=[]、black_spans=[]、audio_ok |
| 品牌可见性 | brand_seen: **true** |
| 逐镜 VLM | S01 pass / S05 pass；S02 1 critical；S03 3 critical；S04 1 critical |
| 终审判定 | verdict=fix（内容层），成片保留为 candidate |

**残留内容 findings（如实记录，均为 H3 生成侧随机性，非确定性门失败）**：
- S02：分镜写「低头看杯」，模型在 2.68s 加了「举吸管喝」——画面合理但与 motion
  文本不符（属剧本措辞与模型行为的偏差，非崩坏）；
- S03：海景空镜在前 ~1.1s 正确，随后漂出咖啡杯特写（去掉 centering 子句后
  从 7 条 finding 降到 3 条，未全清——见 P-17）；
- S04：1 条 critical（本轮新增，未及定位）。

**P-17（新发现，已修）**：视频提示词模板的 "subject stays centered" 对**无主体
镜头**（纯风景空镜）是负向指令——模型会发明一个主体并居中（实测海景空镜
1.1s 后漂出咖啡杯）。已在派生逻辑中对 subject 以「无人物」开头的镜头去掉该
子句（fixes.md F-11），finding 数 7→3。彻底消除需要更强的空镜提示词工程
（建议见 suggestions.md S-11 延长线）。

**结论**：确定性质量门全绿；内容审查按 fail-closed 原则未给 RELEASED。
成片（22.3s、720×1280、双语音轨+字幕+本地 BGM、-14.34 LUFS）已产出在
`final.mp4` / 本报告 `artifacts/final_tvc.mp4`，可直接播放评估。
