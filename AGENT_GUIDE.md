# AGENT_GUIDE — shipin-platform 操作契约（供任意 AI Agent 使用）

> 本文档是平台的"自描述接口"。**任何 Claude / GPT / 自定义 Agent** 第一次接触本
> 平台时，先读本文档（或请求 `GET /api/agent-guide`），即可在零训练、零提示词的
> 情况下把"用户一句模糊意图"变成一条"可审核→可生成→可成片"的流水线。
>
> 核心原则：**每个阶段产出 JSON → 先过 review 关口 → 再进入下一阶段**。
> 评审引擎能自动修的问题服务端自动修；修不了的以 `manual_modes` 交还给 LLM/用户重写。

---

## 0. AI 工作流（MCP 看板执行者，2026-09-18 起外部 AI 统一入口）

外部 AI（Codex / Claude Code 等）通过 `tools/mcp_server.py` 的 **16 个受控工具**
操作平台，禁止直接编排端点（40+ 低层 API 是内部实现）。本平台的 AI 是
**沙盘看板上的执行者，不是决策者**：

- 每个动作之前先 `shipin_project_status` 拿决策上下文——
  `gates_pending`（待确认闸门）、`artifacts`（产物清单含版本数）、`budget`
  （上限 / 已用 / 是否超限）、`recent_events`（谁动过什么）；
- **确认才落闸**：任何产物展示给用户并获得明确确认后，`shipin_project_confirm`
  才可调用（approved_by 如实填写）；不确认服务端 409，AI 不得绕过；
- **改完必须重演链路**：`shipin_rewrite_stage` 改写 → 自动清确认 + 下游全部
  失效 → 重新展示 → 重新 confirm → 才允许再跑下游（杜绝"改完旧链条继续花钱"）；
- **花钱之前先 preflight**：`shipin_preflight` 只读体检（凭据 / 产物 / 闸门 /
  预算 / 目录 / P7 资源面 rate/disk/audit），任一不过就停下向用户报告，不硬闯；
  请求被限流（429）时按 `Retry-After` 等待，勿高频重试；
- 一切改动落在事件时间线（`shipin_list_events` 可回放），AI 没有任何
  代码执行 / 文件 / 命令入口，工具的修改只有人能用
  `tools/platform_integrity.py` 重新封印。

| 组 | 工具 | 用途 |
|---|---|---|
| 会话 | `shipin_health` / `shipin_integrity` | 凭据状态 / 平台封印自检 |
| 观察 | `shipin_list_projects` / `shipin_project_status` / `shipin_list_events` / `shipin_get_artifact` / `shipin_preview_frames` | 项目清单 / 决策上下文 / 轨迹回放 / 阶段产物 / 成片预览帧 |
| 编辑 | `shipin_rewrite_stage` | 改写 script/storyboard → 清闸 + 下游失效 |
| 推进 | `shipin_pipeline_text` / `shipin_project_confirm` / `shipin_preflight` / `shipin_pipeline_generate` / `shipin_pipeline_assemble` / `shipin_report` | 文案 / 确认 / 体检 / 生成 / 成片 / 全量快照 |
| 预算 | `shipin_set_budget` | 项目预算或平台全局月度预算（null 解除） |
| 参考 | `shipin_ingest_reference` | 本地参考视频纵深剖析 → 预填 brief |

**标准工作流**：`health → pipeline_text → 展示 → confirm → preflight(全绿)
→ generate → assemble → report / preview_frames`；中途任何改写走
`rewrite_stage → 展示 → confirm → preflight → 重跑下游`。所有失败均为
`ok:false + 稳定错误码`（如 `GATE_NOT_CONFIRMED` / 预算 422），原样上报用户，
不自行重试 / 绕过。

---

## 0. 受控执行入口（Agent 默认走这里，2026-09-15 起）

Agent 不再需要手工编排后文 §4–§7 的多步调用——那套低层 API（40+ 端点）已收编为内部实现。
默认入口只有 6 个端点（白名单 + 固定顺序 + 审查门 + 台账，代码强制）：

| 步骤 | 端点 | 说明 |
|---|---|---|
| ① 开始运行 | `POST /api/guard/start` | body: `{project_id, brief?}`；返回 run_id + 当前可调函数 |
| ② 执行 | `POST /api/guard/call` | body: `{run_id, function, params}`；只能调当前步骤白名单内的函数 |
| ③ 提交审查 | `POST /api/guard/review` | body: `{run_id}`；通过→自动进入下一步；不通过→停留+原因 |
| — 人工放行 | `POST /api/guard/manual-pass` | 仅该步骤开启且已有失败审查后可用；blocked 唯一解锁 |
| — 看状态 | `GET /api/guard/{run_id}/status` | 我在哪一步/能调什么/审查结论 |
| — 查台账 | `GET /api/guard/{run_id}/events` | 事件流回放（含每次拒绝的原因） |

硬约束（违反会被拒并留痕，响应 `ok:false` + 稳定错误码）：
- 未注册函数 → `UNKNOWN_FUNCTION`
- 跳步/乱序 → `NOT_ALLOWED_IN_STEP`（消息含当前应处步骤）
- 审查未通过时调下一步函数被阻断；连败达上限运行变 `blocked`
- blocked 后一切调用被拒，只有人工放行解锁

**Agent 行为契约**：收需求→start/call/review→把 confirm_required / 审查结论如实转述给用户；
遇到 stall/stop/blocked **原样上报，不自行绕过或重试**。
后文 §1–§7 的低层端点仍对运维/调试开放，但不再是 Agent 的默认路径。

---

## 1. 平台是什么 / 不是什么

| 做 | 不做 |
|---|---|
| 用户意图 → 结构化 brief（`/api/intake/*`） | 不生成图像/视频素材（素材由外部图像/视频生成 API 按提示词产出） |
| 每阶段 JSON 多轮质量审查 + 机械自修复（`/api/review/iterate`） | 不替 LLM 编剧（script/storyboard/prompt 由 LLM 写，平台负责把关） |
| 素材拼接、转场、字幕识别/烧录、音频混音/闪避/响度标准化 | 不阅读理解抖音风控等外部平台规则（`special_requirements` 由用户提供） |
| 质检：ffprobe、黑帧检测、幻灯片风险、场景变化 | 不搬运素材文件；所有路径均为**服务器本地绝对路径** |

## 2. 启动

```bash
cd D:/aishipin/shipin-platform
pip install -e .            # 或 pip install -r requirements.txt
uvicorn src.api:app --host 127.0.0.1 --port 8766   # 服务端口默认 8766
```

依赖：FFmpeg/ffprobe（PATH 中）、OpenMontage（`D:/aishipin/OpenMontage-main/OpenMontage-main`）、whisper（可选，字幕识别用）。

## 3. 术语与审查合同

**阶段 stages**（review 可审的维度）：

```
brief → script → storyboard → image_prompt → video_prompt
```

**decision（每轮审查的结论）**：

| decision | 含义 | 行动 |
|---|---|---|
| `pass` | 通过 | 进入下一阶段 |
| `pass_with_warnings` | 通过但带建议 | 可进入下一阶段，建议顺带优化 |
| `revise` | 需修复 | 服务端已尽力机械修复；下游 LLM 按 `findings` 重写后再 iterate |
| `stall` | 无改善 | 修来修去没有进步——**停下，追问用户/换思路** |
| `stop` | 达到轮数上限 | 读 `manual_modes`：这些维度必须 LLM/用户处理 |

`stall`/`stop` **不是错误**，是平台在告诉你"机械手段到头了，需要人类/LLM 的输入"，这是闭环设计的一部分。

**manual_modes 的语义**：即本轮 `revision.fix()` 之后仍未解决、且**机械规则修不了**的 failure_mode 清单（如 `MISSING_DIMENSION`）。拿到后你应该：向用户追问该维度 → 更新 data → 重新 POST iterate。

**revision_plan 的语义**（每轮 review 都有）：这是把 `findings` 压缩成的「必改 N 条 + 建议 + 锁项」执行单：
- `必改*` 行是硬门槛，逐条落实后再重交；
- `锁项` 行告诉你**不许动**什么——只改清单上的字段，其余 JSON 原样保留；
- 照单一次改完，不要来回试错（轮次越少，越接近一次过闸）。

## 4. 端到端流程（意图 → 成片）

```
用户意图
  │ ① POST /api/intake/questions      ← 该问用户什么（9 个必填维度）
  │ ② 逐题向用户收集答案
  ▼ ③ POST /api/intake/draft          ← 组装 brief（缺失维度给默认值+missing 标记）
brief JSON
  ▼ ④ POST /api/review/iterate (stage=brief) ×N 直到 decision ∈ {pass, pass_with_warnings}
script JSON（LLM 写：shots[]）   
  ▼ ⑤ POST /api/review/iterate (stage=script)
storyboard JSON（LLM 写：镜头/场景）
  ▼ ⑥ POST /api/review/iterate (stage=storyboard)
image_prompt / video_prompt（LLM 逐镜头写）
  ▼ ⑦ POST /api/review/iterate (stage=image_prompt / video_prompt)
素材文件（LLM 调外部生成 API 产出 .mp4/.png 到本地目录）
  ▼ ⑧ 素材验收（硬门禁，逐条不放过）：
       ffprobe 可解码+时长≈设计时长
       POST /api/video/frames 抽帧 → 交 VLM 查物理异常
       （双键盘/双屏/多手/穿模/物件重复/文字乱码 → 该条重拍）
       VLM 通过且画面与提示词语义一致 → 才准进下一步
  ▼ ⑨ PRIMARY POST /api/video/stitch 或 /api/video/concat
       （按镜头时间轴精确裁剪，目标时长±0.2s）
正片（静默画面链）
  ▼ ⑩ 音频设计（按 §10.3 决策树定层，不是无脑套三声）：
       ① 先 probe 每条素材音轨 → 判定原生音轨类型（环境/对白/静音）
       ② 原生音轨保留：与画面拼接对齐后作为环境主轨道（禁 -an 丢轨）
       ③ 对白/角色 → 多音色 TTS 逐角色配音并对齐口型窗口；环境叙事 → 零 VO
       ④ BGM + 动作点 SFX + 房间底噪 分层，POST /api/audio/mix 混入正片
  ▼ ⑪ POST /api/subtitle/burn（font_size=46、margin_v=96，逐cue验收）
  ▼ ⑫ 终验三连（缺一不交付）：
       POST /api/video/probe        → 时长/分辨率/码率
       POST /api/audio/probe      → verdict=='ok' 才放行（有声音、不削波、长静音）
       POST /api/video/black-detect → 无意外黑帧
成品视频（有证可查：probe+audio probe+字幕cue+黑帧全绿）
```

规则：**越早的评审越便宜**。前一阶段 `revise` 时回到该阶段重写，**不要**跨过未通过 stage 直接往下。**素材不合格 → 改提示词重拍，绝不带病拼接**。**音频违反 §10.3 决策树（该留原生音轨而丢弃 / 该配音却无 / 该静默却硬加机器人声）→ 视为"声音不合格"，禁止交付**。

## 5. API 速查

基址：`http://127.0.0.1:8766`（`SHIPIN_API_URL`）。所有 body 均为 JSON。

| 方法 | 路径 | 入参（key） | 返回（key） |
|---|---|---|---|
| GET | `/api/agent-guide` | — | 本手册的 JSON 版 |
| POST | `/api/intake/questions` | `intent?` `answered?`(已答字段) | `questions[]`(dimension/question/required/default) |
| POST | `/api/intake/draft` | `answers`(字段→值), `intent?` | `brief`, `missing[]`, `review`, `next` |
| POST | `/api/review/iterate` | `stage`, `data`(阶段JSON), `max_rounds`(默认3) | `rounds[]`, `decision`, `data`(修复后), `manual_modes[]`, `revision_plan[]` |
| POST | `/api/check/slideshow-risk` | `scenes` | 6 维度滑铁卢风险打分 |
| POST | `/api/check/variation` | `scenes` | 8 项场景变化检查 |
| POST | `/api/subtitle/transcribe` | `audio_path` | `ok`, `srt_path`, `json_path` |
| POST | `/api/subtitle/burn` | `video_path`, `srt_path`, `output_path` | `ok`, `output` |
| POST | `/api/video/stitch` | `clips[]`, `output_path`, `transition`(cut/crossfade/fade), `transition_duration` | `ok`, `output`, `duration` |
| POST | `/api/video/concat` | `clips[]`, `output_path` | `ok`, `output` |
| POST | `/api/video/frames` | `path`, `out_dir`, `interval`(秒), `max_frames?` | `frames[{t,path}]`, `count` —— 素材抽帧（VLM 物理一致性验收的输入） |
| POST | `/api/audio/probe` | `path` | `verdict`/`mean_volume_db`/`lufs`/`silence_segments` —— 声音放行硬门，`verdict=='ok'` 才准交付 |
| POST | `/api/audio/duck` | `primary`, `secondary`, `output`, `duck_level`(负dB) | `ok` |
| POST | `/api/audio/mix` | `tracks[]`({path,role:narration/music/sfx,volume}), `output` | `ok`, `output` |
| POST | `/api/audio/normalize` | `input_audio`, `output_audio`(可省), `target_lufs`(-14) | `ok`, `measured_lufs` |
| POST | `/api/video/probe` | `path` | codec/resolution/fps/duration/audio_codec |
| POST | `/api/video/black-detect` | `path`, `min_dur?`(0.5) | `[{start,end}]` |
| POST | `/api/video/color-grade` | 见下 | — |
| POST | `/api/video/encode` | 见下 | — |

> `color-grade` 参数：`input_path`, `output_path`, `style`(warm/cool/cinematic/bleach_bypass)。
> `encode` 参数：`input_path`, `output_path`, `crf?`, `preset?`, `resolution?`。

**注意**：所有路径为服务器本地绝对路径；`transition` 合法值只有 `cut | crossfade | fade`（fade 指黑场淡入淡出）。

## 6. CLI（等价于 API 的本地入口）

```bash
python src/main.py review brief.json --stage brief --rounds 3   # 输出 rounds + manual_modes
python src/main.py stitch --clips a.mp4 b.mp4 -o out.mp4 --transition crossfade
python src/main.py subtitle --audio a.wav --video raw.mp4 -o cap.mp4     # whisper 转字幕+烧录
python src/main.py analyze in.mp4 --black-detect
python src/main.py pipeline brief.json          # 全流程跑批（仅编排）
```

## 7. 一次完整接管的实操示例（agent 视角）

**用户意图**："我想做一个爽文短剧，每次公交车上看，一定要燃，主角打脸全场。"

```bash
# ⚠ 先看这里：Windows / Git Bash 下请不要 `-d'{"中文":...}'` 直传，
#   curl 会把非 ASCII 转成本地代码页，服务端 JSON 解析直接失败。
#   正确姿势：把 JSON 先写进 UTF-8 文件，用 @file 传（所有示例通用）：
#   printf '{"intent":"爽文短剧，燃，打脸"}' > /tmp/body.json
#   curl -s -XPOST ... --data-binary @/tmp/body.json

# ① 取问题清单
printf '{"intent":"爽文短剧，燃，打脸"}' > /tmp/body.json
curl -s -XPOST http://127.0.0.1:8766/api/intake/questions \
  --data-binary @/tmp/body.json -H'Content-Type: application/json'
#    → 得到 9 个必问项（content_type/duration/tone/creative_direction…）

# ②（与用户对话后）组装
printf '%s' '{"intent":"废物男主被家族当众羞辱后觉醒隐藏身份，当众打脸族长",
             "answers":{"content_type":"short_drama","duration_sec":90,
                        "target_platform":"抖音","tone":"燃",
                        "target_audience":"男频爽文受众"}}' > /tmp/body.json
curl -s -XPOST http://127.0.0.1:8766/api/intake/draft \
  --data-binary @/tmp/body.json
# → brief + missing[] + 一轮 review(decision=revise)

# ③ 补齐 missing 再 iterate
curl -s -XPOST http://127.0.0.1:8766/api/review/iterate \
  --data-binary @/tmp/body.json
# → decision: pass / pass_with_warnings → 放行

# ④ 之后你自己写 script/storyboard/prompt 的 JSON，同样走 iterate 闸门
# ⑤ 生成素材后：
printf '%s' '{"clips":["D:/media/shot1.mp4","D:/media/shot2.mp4"],
      "output_path":"D:/media/final.mp4","transition":"crossfade",
      "transition_duration":0.6}' > /tmp/body.json
curl -s -XPOST http://127.0.0.1:8766/api/video/stitch \
  --data-binary @/tmp/body.json
# ⑥ 字幕：
printf '%s' '{"video_path":"D:/media/final.mp4","srt_path":"D:/media/final.srt",
      "output_path":"D:/media/final_titled.mp4"}' > /tmp/body.json
curl -s -XPOST http://127.0.0.1:8766/api/subtitle/burn \
  --data-binary @/tmp/body.json
# ⑦ 质检：
printf '{"path":"D:/media/final_titled.mp4"}' > /tmp/body.json   # 纯 ASCII 可直传
curl -s -XPOST http://127.0.0.1:8766/api/video/probe -d'{"path":"D:/media/final_titled.mp4"}'
```

## 8. 失败模式处置（agent 决策表）

| 遇到 | 处置 |
|---|---|
| iterate 返回 `stall` | 停止重试；对用户复述不清楚的需求，请求补充例子/参考 |
| iterate 返回 `stop` + `manual_modes` | 逐项重新生成对应字段后再次 iterate |
| `revise` 的 findings 带 `revision_strategy` | 机械项服务端已自动修；策略为人工程序跟随 LLM 重写该字段 |
| ffprobe/stitch 报错 | 检查路径存在性、文件格式（先 probe）；存在外部工具缺失时报 OpenMontage 不可用 |
| transition 传错值 | 只使用 `cut`/`crossfade`/`fade` |

## 9. 安全与边界

- **不定式**：所有引擎调用走确定的参数列表，**不做 shell 拼接**；路径以 `-` 开头的会被拒绝（`_media_arg` 校验）。
- 服务只监 `127.0.0.1`（对接的 TS 后端也强制 localhost）；不要暴露到公网。
- 素材生成外部完成——平台只消费本地文件。
- 每个 review 轮次都有 `rounds[]` 历史，失败可追溯。

## 10. 总导演验货规范（2026-09 实战沉淀，硬门槛）

> 本节是"监督者"在成片交付前逐项实测的验收标准。**每个阶段产出的文件都必须在
> 这里能找到对应验收条款**；找不到条款的产出 = 该环节没做。

### 10.1 字幕规范（burn 前自检 / 交付时复核）

| 项 | 规则 | 实测示例（1920×1080） |
|---|---|---|
| 字号 | ≤ 屏幕高度 5%（1080p 下 ≤ 54px；推荐 46px） | 46px = 4.26% |
| 行数 | ≤ 2 行，单行 ≤ 22 个汉字（22 单位 ≈ 11 汉字，按全角宽计） | — |
| 屏占比 | 整块字幕宽度 ≤ 62% 屏宽（避免占满屏） | 7 条 cue 实测 25%–49% ✓ |
| 底部边距 | 下缘距底 ≥ 96px（≈8.9% 屏高安全区），`MarginV` 同值 | y 底 874–974 ✓ |
| 描边 | 白字 4px 黑边 + 2px 投影 | — |

自检手段：`drawtext` 渲染后逐 cue 提取包围盒（PIL：非黑像素 x/y 极值），宽度 ≤62%W、高度 ≤2×1.25 字号，全部通过才准交付。

### 10.2 素材验收（图片/视频/音频三件套，禁止"没有产出"，禁止"带病拼接"）

每个分镜必须同时有：

1. **参考图**（前置概念板，`refs/sbNN.png`）：1920×1080 PNG，深色底板、电光蓝点缀、产品剪影、右下角标注
   `NN · 镜头 · 概念参考图（占位）`——占位必须显式标注，不得冒充成片素材。
2. **视频片段**（`media_v2sbNN.mp4`）：每镜 1 条，非空封装（>20KB）、可解码、时长≈分镜时长。
3. **提示词**（image/video 双份 JSON）：逐镜挂接参考图路径；视频提示词 = 机位（static/dolly/track/orbit/crane）+ 时长 + 情绪，禁 zoom 类词。

**10.2.1 物理一致性验收（硬门禁，2026-09 血泪教训）**

AI 生成视频最常见的"带病镜"：**一件物品被生成两次**（双键盘、双屏幕、双屏笔记本）、手指/肢体数量错误、物件穿模、文字乱码成花纹。这类镜头放生成阶段的
抽样检查里极难发现（生成器在 5s 里前 2s 正常、后 3s 出现畸形）。

验收步骤（对**每条**视频片段执行，缺一不准进入拼接）：

```bash
# 1) 基础可解码性（全部通过才继续）
ffprobe -v error -show_entries stream=codec_name,width,height,duration -of json out.mp4

# 2) 抽帧 —— 用平台 gate 工具，逐秒抽帧
curl -s -XPOST http://127.0.0.1:8766/api/video/frames \
  -H 'Content-Type: application/json' \
  --data-binary '{"path":"D:/abs/out.mp4","out_dir":"D:/abs/frames_sbNN","interval":0.8}'
# → {frames:[{t,path}], count:N}

# 3) 把全部帧交给多模态模型，逐张回答固定 4 问：
#    [物理一致性] 画面中每个实体是否只出现一次？（单键盘/单屏/单只手/单台电脑）
#    [结构合理性] 有无穿模、悬空、变形、数量错误？
#    [语义匹配] 画面主体是否符合该镜提示词？
#    [临场缺陷] 黑屏/花屏/文字乱码/水印？
#    任一帧有一项不过 → 该条重拍（见 10.2.2），本镜头不进入拼接。
```

判决口径：
- **任何一帧出现"同物双份"（双键盘/双屏/多手/重复人形）→ 直接 fail，重拍。**
- 重拍提示词必须在镜头原语上加物理约束句：`exactly one X, single Y, no duplicate, no morphing`，
  并加强 `scene invariance`（同一场景/同一物件/同一服装，防止风格漂移）。
- 二轮仍 fail → 换参考图种子 + 加"静态机位"约束（动态镜头是畸形高发区）。

### 10.3 音频架构（决策树：原生音轨优先，禁止默认机器人配音）

**第一原则——AI 生成视频自带原生音轨，必须保留使用，禁止 `-an` 丢弃**：
每条 AI 视频素材落盘后**第一步** `POST /api/audio/probe` 该素材（素材清单位：
`test_out/reals/vids/<sbNN>.mp4`，全部 aac 48k 立体声、含真实环境声/动效），
随后拼接时用 `-map 0:a` 把原生音轨按镜头时间轴对齐连成**原生环境床**。
原生床就是"原视频对应的音频"——AI 生成器往往带上了与画面匹配的环境声
（翻找声/合盖声/人潮声/呼吸灯声），这是任何合成 TTS 都替代不了的真实感。

**第二原则——音频分层决策树（拼接后、混音前执行）**：

| 素材音轨内容 | 主声层选择 | 旁白策略 |
|---|---|---|
| 原生音轨含有效环境/动效声 | **原生环境床为主轨**（提亮到 −20~−22 LUFS 作环境底） | **禁止默认 TTS 旁白**。字幕承担文案；只在 brief 明确要求解说时才加旁白 |
| 画面含角色说话/口型 | 原生音轨保留 + **多音色 TTS 逐角色配音**，每条入点=口型开启帧，时长不得超出说话窗口（见 10.4） | 不同角色用不同音色（男/女/老/少分色），并校准速率使音频时长≈口型时长 |
| 素材本身静默（probe 报 mute） | 无原生可用 → 房间底噪 + 动作 SFX 撑起环境层 | 再判 brief：要解说才 TTS，否则环境叙事片保持无声叙事 |

**第三层 —— 环境音必须"细致"，三层堆叠不许一层糊**：

1. **原生环境床**（上面连出来的，真实场景声）——主环境。
2. **关键动作音效 SFX**：合盖"啪"、开盖按键、"乱找"音响等动作瞬态，按分镜时刻精确
   `adelay=<毫秒>` 对准画面动作，**不要一次性铺满整段**（用瞬态点，不用持续层）。
3. **房间底噪/音乐垫**：房间哼（`room_hum.wav` 60s 低频粉噪）与 BGM 垫底，音量远低于
   环境床，保证全程有"空气感"而不喧宾。

**混音命令**：`POST /api/audio/mix`，tracks 依次为 原生床/BGM/SFX 各轨，
`normalize:false` 交给最终归一化，混完 `POST /api/audio/normalize` 校到 −14 LUFS。

**终验硬门（audio probe，verdict 必须 == 'ok'）**：

```bash
curl -s -XPOST http://127.0.0.1:8766/api/audio/probe \
  --data-binary '{"path":"D:/abs/final.mp4"}' -H 'Content-Type: application/json'
```

| verdict | 含义 | 处理 |
|---|---|---|
| `ok` | 有声、未削波、无 >4s 连续静音 | 放行 |
| `mute` | 无音轨或数字静音 | **禁止交付**：回查丢轨（拼接是否 `-an`）或补环境层 |
| `too_quiet` | mean < −30dB（≈听不见） | **禁止交付**：原生床提亮或补环境层重混 |
| `silence_windows` | 存在 >4s 连续安静 | 核查是否设计空拍（静止神/收束镜允许 1 次）；非空拍禁止交付 |

> ⚠ 只在"done 之前"能听到声音还不够；**交付前最后一次完整 audio probe = 声音验收放行条**。
> 声画时长差 >0.3s（音轨比画面短/长）也在 audio probe 里暴露——回混音重做。

### 10.4 多音色配音与 SFX（TTS 只在"明确需要人声"时启用）

**配音角色数据库（v0.5 起必查）**：平台维护 SQLite 角色库
`data/voice_cast.db`，同一角色永远映射同一 edge-tts 音色。配音前必须先查：

```bash
curl -s http://127.0.0.1:8766/api/cast/roles     # 谁用哪个音色/风格
curl -s http://127.0.0.1:8766/api/cast/script    # 整片台本（谁×何时×说什么）
# 新增台词：POST /api/cast/assign {role_code, q_idx, start_s, end_s, text}
```

内置角色（可按项目扩展）：
| role_code | 音色 | 定位 |
|---|---|---|
| `hero_male` | zh-CN-YunxiNeural (−8%) | 男主第一人称，产品价值句/收束情绪 |
| `biz_female` | zh-CN-XiaoxiaoNeural (−6%) | 女声旁白，钩子/转场/总结 |
| `colleague_male` | zh-CN-YunjianNeural (−6%) | 配角男声，慌乱/对比段 |
| `assistant_female` | zh-CN-XiaoyiNeural (−6%) | 配角女声，短反应句 |

**多角色配音原则**：同一角色一色到底，角色之间音色必须不同（男/女/老/少分色）；
一句台词只属于一个角色，严禁旁白同时覆盖产品价值句（那是"机器人旁白"的听感来源）。

**什么时候可以用 TTS 旁白**：brief 明说要有解说词 / 画面含对白角色需要配音。
**什么时候坚决不用 TTS**：素材原生音轨已含可听环境声且无角色说话的必要场景——保留原生声为主，配音只点睛。

**台词配音命令（必须先从 /api/cast/roles 取音色，禁止自创）**：

```bash
# 男一号（hero_male）：YunxiNeural 语速放缓
edge-tts --voice zh-CN-YunxiNeural --rate=-8% \
  --text "一公斤，是拿得起放得下的重量。" --write-media vo_cue1.wav
# 女声旁白（biz_female）：XiaoxiaoNeural
edge-tts --voice zh-CN-XiaoxiaoNeural --rate=-6% --text "…" --write-media vo_f1.wav
```

- 对白配音：**角色一音色用到底**（同一人不得中途换声），TTS 时长 ≤ 口型开角窗口，
  超出用 `atempo=1.05..1.2` 收速或重写台词，严禁超窗挂空。
- **VO 时间轴 = 画面 cue 时间轴**：每条 VO 入点对应字幕 cue 的 start；各句独立 wav
  交错排列，合起来才覆盖全片节奏。

**SFX 音效生成（无素材库时用 ffmpeg 合成，放 `test_out/_tools/sfx/`）**：

```bash
mkdir -p test_out/_tools/sfx
# 合盖"啪"：80ms 粉噪衰减
ffmpeg -y -f lavfi -i "anoisesrc=color=pink:duration=0.08:amplitude=0.6" \
  -af "afade=t=out:st=0:d=0.07,volume=0.7" test_out/_tools/sfx/click_lid.wav
# 键盘敲击：20ms 棕噪 click
ffmpeg -y -f lavfi -i "anoisesrc=color=brown:duration=0.02:amplitude=0.9" \
  -af "volume=0.5" test_out/_tools/sfx/click_key.wav
# 环境底噪：60s 高频粉噪垫（作为"空气感"）
ffmpeg -y -f lavfi -i "anoisesrc=color=pink:duration=60:amplitude=0.05" \
  -af "volume=0.25,lowpass=f=400" test_out/_tools/sfx/room_hum.wav
```

SFX 入 `/api/audio/mix`（role=sfx）；最终验收口径同为
`/api/audio/probe` verdict==ok。
注意：验收以**混音后的成片**为准——单个 SFX 素材（尤其环境底噪）单独 probe 报
`too_quiet` 属预期；成片总响度达标才放行，禁止单条素材重拍。

### 10.5 本机已知缺陷与绕行（Windows + 本 FFmpeg）

| 缺陷 | 表现 | 绕行 |
|---|---|---|
| libass 不渲染字幕 | subtitles 滤镜/ASS/SRT 全部静默无字 | `POST /api/subtitle/burn` 已内置自愈：启动时探测一次，探测/渲染后逐 cue 验证墨迹，无字自动回退 drawtext（相对路径字体 + `enable=between(t\,a\,b)`） |
| 滤镜图里冒号 | 路径 `C:/…` 的冒号被当选项分隔 | 平台 drawtext 路径全部走 CWD 相对路径（无冒号）；filter_complex 需 `[0:v]…[vout]` 显式绑定输入输出 |
| CJK 字体缺失 | drawtext 中文变豆腐块 | 平台自带 `test_out/_tools/fonts/msyh.ttc`，未找到时自动从 Windows 字体复制 |

### 10.6 /api/subtitle/burn 端点新契约（2026-09 起）

请求体（合规默认值，无需传即达标）：

| 字段 | 默认 | 说明 |
|---|---|---|
| `font_size` | 46 | ≤5% 屏高（旧默认 18 与规范冲突，已废） |
| `margin_v` | 96 | 底部安全区（旧默认 50 太贴底，已废） |
| `mode` | `auto` | `auto`（探测+自愈）/ `drawtext` / `subtitle` |

返回新增客观度量字段（判断权仍在调用方）：

```json
{
  "ok": true, "output": "…", "strategy": "drawtext|subtitle",
  "libass_probe": {"libass": false, "detail": "glyphs-absent"},
  "cues": [
    {"index": 1, "start": 0.0, "end": 3.0,
     "found": true, "changed_px": 8651, "width_pct": 34.2,
     "rows": 1, "y_range": [929, 973]}
  ]
}
```

验收口径：所有 cue `found=true` 且 `width_pct ≤ 62` 且 `y_range` 下缘 ≥ 屏高−110px。

**假通过免疫**：若渲染后逐 cue 验证零墨迹（`changed_px` 全 0），端点拒绝返回成功——`auto` 下先回退 drawtext 再验；连 drawtext 都无字时报 500 并说明，保证不再出现”字幕 burn 返回 ok 但视频无字幕”。”

### 10.7 TVC 质感规范（对照官方联想与 AI 博主案例，2026-09 版）

> 背景：成片不能只是”素材+配音”。对比联想官方 ThinkPad/Yoga 广告与头部 AI
> 博主（Seedance 全流程、MiniMax 短片 Skill）的作品，商业 TVC 有 7 个语言要素
> 是素材链+字幕给不了的。**以下每一条都是成片必查项，缺一即回炉**。

| # | 要素 | 官方/AI 博主怎么做 | 我们的合格口径（可验证） |
|---|---|---|---|
| 1 | **四拍结构** | 钩子(3s)→冲突/痛点→产品价值(3连)→落版(≤5s)；全程一条节奏线，不是素材罗列 | 分镜 JSON 里明确标注 4 拍起止；落版段必须存在且 ≠ 普通镜头 |
| 2 | **剪辑节奏** | 快切/卡点（动作帧对齐乐句）、推拉/升格、匹配转场；平铺=不合格 | 拼接 plan 里有转场；至少 1 处”静→动”或”快→慢”节奏对比；卡点处音画对齐(±0.1s) |
| 3 | **品牌落版** | 结尾 2-5s：slogan 大字 + logo + 音效收尾，”气口”留给落版 | 结尾段有独立大字标题（非字幕），有专属音效落点，落版后无旁白争夺 |
| 4 | **文案入画** | 短句大字焦点词（”一公斤””轻”），一句话只传达一个点；不是长句铺满脸 | 每 cue ≤ 12 字（必要时分两行）；至少 1 屏有焦点词放大样式 |
| 5 | **音效纵深** | 环境床+动作点+低频垫三层，动作点与画面帧对齐；场景响度有起伏 | SFX 点位 ≥3 且全对齐动作帧；全片响度包络非平坦（可听可判） |
| 6 | **BGM 情绪曲线** | 开场低频/中段驱动/落版收束或斩断；不循环一条平铺 | BGM 有 ≥3 段音量/情绪曲线，高潮段明显提速或有加层 |
| 7 | **光色一致性** | 官方锁固定光位/配色；AI 博主用参考图+统一色调 | 全部素材经统一色彩预处理，相邻镜头色温差小 |

**执行顺序（画面上线前就把”质感”做进计划，不是最后补救）**：

1. storyboard 阶段每镜必须带 `beat`（钩子/痛点/价值/落版）、`rhythm`（快/慢/中快）、
   `sfx`（这一镜该有的声音）；缺这三个字段 → review 直接 revise。
2. 拼接阶段：规划转场（哪两镜间 crossfade/闪切）、落版段预留 2–5s 专用
   大字卡片段（video_prompt 直接生成或后期叠加）。
3. 统一光感：所有素材先过 `/api/video/color-grade`（同一 style）再拼接。
4. BGM 分段：至少 3 段（intro/body/outro），段间用音量包络或不同 track 接续。
5. 落版：最后的 slogan 大字 + logo + 收尾音直接叠加，不进字幕 burn
   （字幕 burn 只负责说话字幕）。
6. 逐条走 §10.8 终验清单。

### 10.7.1 故事弧线规范（五幕因果链，2026-09 v7 实测版）

> 背景：v5/v6 被判定为“几个镜头拼接、上下无关联”——逐秒映射审计发现原因：
> 分镜只是“素材罗列”（同一素材反复复现多个位置、无因果顺序）。v7 重构成五幕
> 因果弧线后，VLM 抽帧验收通过（14 帧全部命中对应幕次，无断点）。
> **判别标准：把全片时间轴按幕打印出来，任何人只读 sequence 顺序就能复述故事；
> 做不到 → 重排素材顺序，而不是加字幕掩盖。**

1. **五幕骨架**（60s TVC 固定结构，缺失即不合格）：
   - 钩子（0–4s）：一个反常细节强制目光停留（细节特写，不与痛点抢戏）
   - 痛点（约至 1/3 处）：日常场景里的冲突（多设备混乱、赶时间、开会迟到）
   - 转折（1/3–2/3）：产品介入带来第一个动作转折（合盖/拎起/静音），
     痛点在此“断掉”，画面安静下来（静→动的对比点）
   - 延展/验证（2/3–4/5）：产品价值连续兑现（开盖即用/降噪/轻携带），
     场景逐步升级（桌面→走廊→城市）
   - 收束落版（结尾 4–6s）：深夜/独处等情绪镜头回扣开篇意象 + 大字落版
2. **因果链字段**：storyboard 里相邻幕之间必须可回答“为什么下一幕发生”。
   `cause`（上一幕给了什么理由）+ `effect`（本幕因此如何变化）在分镜 JSON
   里成对出现；审查时按 cause→effect 串联朗读，断链 = 素材没排对位置。
3. **回环与镜像**：收束幕必须“回扣”开场意象（例如开场混乱桌 ↔ 收尾安静桌、
   开场合不上包 ↔ 收尾单指拎起），让首尾构成闭环；**只回扣一次**，不做重复堆叠。
4. **镜头复用红线**：同一素材（含同机位同景别）全片出现 ≤3 次；
   第 2 次复用必须靠近回环位置且间隔 ≥20s；第 3 次视为危急，禁止。
5. **剧本先于剪辑**：先写 10 句左右的台词（含旁白）+ 按幕排 sequence（素材 id
   顺序），再剪时间轴。sequence 顺序可让子代理无需看画面即可复述剧情。
6. **验收方式**：交付前按 1–5s 间隔抽帧（含幕边界前后各一帧），用 VLM（ev 带图）
   逐幕核对“连续性/断点/是否与台词对得上”。任何一帧被判定“与前后无关” → 回炉。

> 台词短句规范（§10.7 第 4 条细化）：旁白 ≤ 14 字/句，两行大字 ≤ 12 字/行；
> 台词只在“该发声的时刻”出现——动作/转场处静，给 SFX 和画面留气口。

### 10.7.2 硬门操作手册（子代理必读，2026-09 实战）

> 背景：规则只写出来不够——v5 的审核就是"文档有但引擎不执行"的典型。
> 下面三节是**可复制的操作手册**，子代理拿到任何一个 fail verdict 时，
> 按这里写的动作修，不要靠猜。

#### A. 何时调用哪些门（调用树）

| 阶段 | 调用端点 | 何时调 | 失败后回哪步 |
|---|---|---|---|
| storyboard 写完 | `POST /api/review/storyboard` | 写完分镜 JSON 后必调 | revise → 按 findings 改 JSON 重交 |
| 拼完时间轴后 | `POST /api/review/timeline` | 生成剪辑计划后必调 | revise → 按 findings 调素材位置 |
| 成片完成后 | `POST /api/review/final-video` | 全部渲染后终验必调 | fix → 按 VLM breaks 重剪或补过渡 |

**调用顺序不可颠倒**：timeline 必须在 storyboard pass 之后、最终渲染之前调用；final-video 是最后一步，前两步没过就直接 stop。

#### B. storyboard 硬门 —— 怎么修（按 finding.code）

| code | 根因 | 具体修法 |
|---|---|---|
| `ARC_INCOMPLETE` | 缺某拍 | 在对应位置加一镜，并在 shot 对象里写 `beat: "钩子"/"痛点"/"转折"/"收束"`（四选一，不用全标） |
| `BEAT_FIELDS_MISSING` | 某镜缺 beat/rhythm/sfx | 补三字段，示例：`{"beat":"痛点","rhythm":"快","sfx":"键盘杂音"}` |
| `SHOT_REUSED` | 同一素材复用超限 | ① >3 次：删掉超出部分换同场景其他素材；② 间隔 <20s：把第二次挪到结尾回环位 |
| `TIMELINE_DISORDER` | 拼接顺序反了 | 重排 timeline clips 的 at 字段使其单调递增 |

#### C. timeline 硬门 —— 怎么修

| code | 修法 |
|---|---|
| `REUSE_TOO_CLOSE` | 第 2 次复用距第 1 次 <20s：把它挪到结尾回环位（通常在 40–55s 区间） |
| `REUSE_LIMIT_EXCEEDED` | 同一 src 出现 ≥4 次：删除多余出现，只保留首尾各一次 |
| `TIMELINE_DISORDER` | 重新按 at 升序排列 clips |
| `DURATION_MISMATCH` | 补齐缺失段（加占位或延长末镜）；目标= 脚本 duration_sec |

#### D. final-video VLM 门 —— 怎么修

返回 `verdict=fix` 时会带 `breaks` 数组，每条是断点描述，格式类似：

```
"N.Ns→N.Ns 明显断帧：XXX 场景突然跳到 YYY 场景，主体/色调完全改变，无过渡"
```

**修法**：
1. 把断点位置映射到时间轴（例如 `37.5s→45.0s`）
2. 在时间轴的对应位置**插入过渡段**：
   - 方案 1（推荐）：用现有素材中场景相近的片段做 crossfade（transition=0.8s）
   - 方案 2：补一个空镜头（黑场/模糊/光晕，1–2s），让视觉有个"气口"
   - 方案 3：重排时间轴，把相邻幕的镜头按因果顺序重新排列
3. 重渲后再跑一次 `/api/review/final-video`，直到 `verdict=pass`
4. **VLM 说 "brand_seen=false"**：检查落版镜头是否被字幕 burn 覆盖，或落版 srt 的 `start_sec` 是否在影片最后 4–6s 内。

#### E. iterate 的 blocked 字段语义

```
POST /api/review/iterate 返回 {decision: "stall"|"stop", blocked: true}
```

**必须执行**：
- 停止当前分支，回到产生该 stage 的上游步骤
- 按 `revision_plan` 里的 `必改 N` 条目重写数据
- 重新迭代；**绝对不允许忽略 blocked=true 继续往下走**

#### F. AGNES_KEY 缺失处理

`/api/review/final-video` 返回 `verdict=blocked, reason="AGNES_KEY 未配置"`：
- 检查 `%TEMP%/agnes_key.txt` 是否存在，或 `AGNES_KEY` 环境变量是否已设置
- 没有 key 则 **禁止交付成片**，必须先在平台侧配好 agnes 密钥
- 这是安全红线：没有视觉复审就不许放行

> 以上操作手册与 VLM 审查逻辑一致——任何 fail/blocked 必须**先修再验**，
> 不能跳过或打口头折扣。这是 v7 实测总结的血泪教训。

### 10.8 终验清单（交付前逐项打勾，缺一不交）

| # | 检查 | 工具/命令 | 通过标准 |
|---|---|---|---|
| 1 | 总时长 | `/api/video/probe` | 目标时长 ±0.2s |
| 2 | 分辨率/帧率 | `/api/video/probe` | 1920×1080 / 24fps（或 brief 指定） |
| 3 | 无事故黑帧 | `/api/video/black-detect` | 无意外黑帧（设计黑场除外） |
| 4 | 声音存在且合格 | `/api/audio/probe` | `verdict==ok`，`has_audio==true`，`mean_volume_db ≥ −30` |
| 5 | 音频架构合规 | 按 §10.3 决策树：原生音轨未丢（拼接时 `-map 0:a`）、环境层三层分明、需要人声的场景才有 VO | 能听见场景原生声（非纯合成铺底）；zero 机器人旁白（除非 brief 要求） |
| 6 | 响度 | `/api/audio/normalize` 返回 | ≈ −14 LUFS |
| 7 | 字幕逐 cue | `/api/subtitle/burn` 返回 cues | 全部 found && width_pct ≤62 && 下缘 ≥ 屏高−110px |
| 8 | 素材无畸形 | 拼接前逐镜 `/api/video/frames` + VLM | 无同物双份/穿模/多手/花屏 |
| 9 | **四拍结构** | 分镜 JSON + 拼接 plan | 钩子/引入/价值/落版四段各有明确起止，缺一不交 |
| 10 | **剪辑节奏** | 拼接 plan + 抽查 3 帧 | ≥1 处慢快对比/转场；卡点偏差 ≤0.1s |
| 11 | **落版视觉** | 末 5s 帧目视 | slogan/logo 大字可见，非纯字幕；有专属收尾音 |
| 12 | **文案入画** | 字幕文件目检 | 每 cue ≤12 字，≥1 屏大字焦点样式 |
| 13 | **BGM 曲线** | 混音 plan 记录 | BGM ≥3 段曲线，结尾收束 |
| 14 | **光感统一** | color-grade 执行记录 | 全部素材同 style 处理；抽查相邻帧色差小 |
| 15 | **时间轴复用门** | `POST /api/review/timeline` | `verdict==ok`：同一素材 ≤3 次、回环间隔 ≥20s、时间轴单调、覆盖与目标时长偏差 ≤3%（防 v5 拼接感复发） |
| 16 | **成片 VLM 门** | `POST /api/review/final-video` | `verdict==pass`：无幕断点、逐幕连贯、落版可见；`blocked`（未配 AGNES_KEY）与 `fix` 同样禁止交付 |

> 本清单是”总导演验货”的最后一步；**任何一项不满足，回对应环节重做，禁止带病交付**。

---

*本文档与 `GET /api/agent-guide` 返回内容保持同一份契约；改契约时两处同步。*
### 10.9 平台执行层修复记录（2026-09-07 实测发现并已修复）

| # | 缺陷 | 表现 | 修复 |
|---|---|---|---|
| 1 | `/api/audio/mix` 混音音量不叠加 | OpenMontage `_mix` 的 amix 缺 `normalize=0`，默认除以轨数 → 混后比单轨还轻（实测 −18.6 → −31 LUFS） | `tools/audio/audio_mixer.py` `_mix` amix 加 `:normalize=0`（与 `_full_mix`/`_segmented_music` 一致） |
| 2 | `/api/audio/normalize` 输出 `.wav` 实为 AAC 容器 | probe 报 `unreadable` | 按输出扩展名选编码：`.wav`→`pcm_s16le`，否则 AAC |
| 3 | `/api/audio/normalize` two_pass 无效 | ffmpeg≥6 loudnorm 默认 `print_format=none` 不打印测量 JSON，正则抓不到 → 静默回退单遍（−14 目标只能到 −17.6） | pass1 加 `:print_format=json` |
| 4 | `/api/subtitle/burn` drawtext 报 Invalid argument | cue 文本文件在 cwd 外 → `relative_to(cwd)` 失败 → 盘符路径 `D\:` 转义被 ffmpeg 拒 | drawtext 路径 cue 目录改到 cwd 内，textfile 恒为无盘符相对路径 |
| 5 | 成片只有"后期机器人 TTS 旁白"，原生音频被丢弃 | 拼接时 `-an` 丢了 AI 素材自带的环境音轨（现 16 段素材全部 aac 48k 带声），又用 edge-tts 单音色铺满全程 → 用户判定"配音是后期配的机器人那种"，环境音也不细 | ① 拼接一律保留 `-map 0:a` 连出原生环境床；② 环境叙事片（无对白无口型）**默认零 TTS**，字幕承担文案；③ 有角色讲话才多音色配音并对齐口型窗口；④ 环境音按 §10.3 分层：原生床 + 动作点 SFX（adelay 对准）+ 低频垫 |
| 6 | 用户回归"完全没有人物声音，纯音乐感" | v4 按"零 TTS"策略跑过头，成片只剩环境+音乐，像"视频加配乐" | ① 建配音角色数据库 `data/voice_cast.db`（`/api/cast/roles` 查角色→音色映射，同角色同音色、多角色分色，禁机器人单音色）；② 环境叙事片也按台本分角色配音（hero/biz/colleague/assistant 四色），人声+原生床+SFX+BGM 四层合成 |
| 7 | 成片停留在"素材+配音"，缺 TVC 质感（无四拍结构感/落版/短句大字/动态 BGM/统一光感） | 与联想官方 TVC、AI 博主作品对比，缺 7 项：四拍结构、剪辑节奏转场、品牌落版（slogan 大字+logo+收尾音）、短句大字文案、音效纵深（定点 SFX≥3）、BGM≥3 段情绪曲线、光感统一 | 新增 §10.7 TVC 质感规范 + §10.8 终验清单 14 项；实测按规范补 v6：三段落版大字卡（slogan 两行≤12字 + 品牌小字行，`/api/subtitle/burn` font_size=84/34、margin_v 垂直定位）、BGM 三段曲线（intro 0.05 弱起→body 0.16→outro 0.10 收束+58s 淡出）、56.3s 双音收尾音、全程 `warm_tvc` color-grade；1120 实测成片 60s/1080p24/−14.4 LUFS/无黑帧/9 cue 全 found + 落版可见；**本机 libass glyphs-absent，字幕与大字一律走 drawtext（msyh.ttc），禁 ASS 叠加** |
| 8 | 成片"没有故事感/剧本设计不合理/上下毫无关联，感觉只是几个镜头拼接"（用户原话） | 分镜等于素材罗列：v5 逐秒映射显示 sb12b 被反复复现、无因果顺序；单镜头无幕次归属；台词在该发声的时刻沉默 | 以 v7 实测重构：① storyboard 每镜标注 `beat/rhythm/sfx` 字段；② 五幕因果骨架（钩子→痛点→转折→延展→收束落版）写进 sequence，相邻幕带 `cause`/`effect` 成对字段；③ 收束幕必回扣开篇意象（首尾闭环）；④ 镜头复用红线 ≤3 次，第2次仅用于回环且距上次 ≥20s；⑤ 台词"该发声时刻"表：动作处静、转场处留气口，旁白 ≤14 字/句；⑥ 交付前 VLM 逐幕抽帧验收（v7 实测 14 帧全命中无断点）；⑦ 规范落 §10.7.1，agent-guide API 同步 |
| 9 | 审核规则只写在文档里、引擎不执行——"审核太容易放过" | `review_storyboard` 只检查"字段齐不齐/景别邻接"，完全没实现 §10.7.1 的五幕骨架/素材复用/旁白短句；成片层只有 probe/black-detect 这类格式检查，无任何内容级视觉闸门；v5 的拼接病可以在 storyboard 关全绿放行 | ① engine.py 新增三个硬门：`ARC_INCOMPLETE`（五幕缺一拍 CRITICAL）、`BEAT_FIELDS_MISSING`（每镜缺 beat/rhythm/sfx CRITICAL）、`SHOT_REUSED`（复用超限 CRITICAL）；② 新增 `/api/review/timeline` 确定性时间轴门（素材复用 ≤3、回环 ≥20s、单调递增、覆盖时长偏差 ≤3%）；③ 新增 `/api/review/final-video` VLM 视觉门（每批 ≤4 帧 ×12 帧，五幕连续 + 字幕可读 + 落版可见；AGNES_KEY 缺失返回 blocked 而非静默放行）；④ script 层加 `NARRATION_TOO_LONG`（旁白单句 ≤14 字）；⑤ iterate 在 stall/stop 时额外返回 `blocked=true` + `blocked_reason`，下游禁止跳过；⑥ §10.8 清单从 14 项扩到 16 项；⑦ agent-guide API `flow[10]` 更新、新增 `hard_gates` 描述 |

**音频链标准配方（v4 实测 60s 成片 → −14.5 LUFS，原生音轨保留版）**：
1. **原生环境床**：拼接视频与原生音轨用同一 filter（`[i:v]… [i:a]atrim/setpts/aformat…` + `concat n=… v=1:a=1`），得到声画同轴的整片原生音轨；
2. 关键动作 SFX 用 `adelay=<毫秒>` 逐点对准画面瞬态（合盖 16.5s、开盖 22.5s、收尾按键 55s），不要整段铺；
3. 房间哼（`room_hum` volume≈0.05）+ BGM（`bgm60.wav` volume≈0.16）垫在原生床之下作"空气感"，全程不抢环境；
4. `amix=inputs=N:duration=longest:normalize=0` + `alimiter=0.95`（避免合成过冲）→ 一轨 raw；
5. `/api/audio/normalize`（two_pass=true, target −14, TP −1.4）→ probe 复核 ≈ −14；
6. 若 final probe 的 `max_volume_db` 已达 −1.4 而 LUFS 仍 −16 左右，说明 TP 锁死 → 降 BGM/SFX 垫底电平重跑第 3–5 步。

### 11. 项目状态机操作 SOP（子代理必读，2026-09 硬门落地）

> 背景：过去审核规则只写在文档里、引擎不执行——任何子代理都能直接跳到最后一步生成成品。本章节描述**真正的流程阻断机制**，不是"建议"，而是强制执行层。

#### 11.1 调用顺序（不可跳步）

```
用户一句话
  ↓ POST /api/project/create {project_id}          ← 创建项目记录
  ↓ POST /api/intake/questions + /draft            ← 收集 brief（无 project_id 可继续）
  ↓ POST /api/review/iterate stage=brief project_id={id} ×N 直到 pass
    → 通过时：POST /api/project/{id}/stage/brief 已自动 record_artifact(PASS)
  ↓ LLM 写脚本（按 §10.7.1 五幕弧线 + cause/effect 字段）
  ↓ POST /api/review/iterate stage=script project_id={id}
    → 通过时：script 阶段 PASS，artifact hash 记录
  ↓ LLM 写分镜（每镜 beat/rhythm/sfx 齐全）
  ↓ POST /api/review/iterate stage=storyboard project_id={id}
    → 通过时：storyboard 阶段 PASS，artifact hash 记录；
              此时 ↓ stitch / burn / finalize 均可调用
  ↓ POST /api/video/stitch clips[] output project_id={id}   ← 硬门：需 storyboard PASS
  ↓ POST /api/review/timeline timeline project_id={id}     ← 硬门：素材复用≤3、间隔≥20s
  ↓ 音频设计 / subtitle burn / color-grade / normalize
  ↓ POST /api/subtitle/burn video_path srt_path output project_id={id}
    ← 硬门：需 video_gen PASS
  ↓ POST /api/review/final-video video_path               ← 硬门：VLM 逐幕验收
  ↓ POST /api/project/{id}/finalize                     ← 全部 REQUIRED stage PASS 才可返回 RELEASED
```

#### 11.2 失败 code → 修复动作速查

| 返回 | code | 修复动作 |
|---|---|---|
| `STAGE_NOT_STARTED` | 上游阶段未 record_artifact | 先 `POST /api/review/iterate` 直到 decision=pass |
| `STAGE_NOT_PASS` | 上游被设为 BLOCKED/STALL | 读 `revision_plan` 的必改项，重写后重新 iterate |
| `HASH_MISMATCH` | storyboard 被改过但下游仍引用旧产物 | invalidate_downstream 会自动把下游标 BLOCKED；重新生产下游产物 |
| `PROJECT_NOT_FOUND` | 调用了不存在的 project_id | 先 `POST /api/project/create` |
| `REUSE_TOO_CLOSE` | timeline 中同一素材间隔 <20s | 把第二次复用挪到结尾回环位 |
| `REUSE_LIMIT_EXCEEDED` | 同一素材出现 ≥4 次 | 删除多余出现，只保留首尾各一次 |
| `ARC_INCOMPLETE` | storyboard 缺少某拍 | 在对应位置加一镜并标 `beat` |
| `BEAT_FIELDS_MISSING` | 某镜缺 beat/rhythm/sfx | 补三字段 |
| `UPSTREAM_FAILED` | finalize 时某必需 stage 非 PASS | 按 `fails[]` 列表依次通过；先修 brief → script → storyboard → video_gen → post_production |

#### 11.3 stall/stop/blocked 处置

```
POST /api/review/iterate 返回 {decision: "stall"|"stop", blocked: true}
→ 立即停止当前分支
→ 读 revision_plan 里的「必改 N」条目重写数据
→ 重新 POST iterate
→ 绝对不允许忽略 blocked=true 继续往下走
```

#### 11.4 用户介入点

**唯一**允许向用户追问的时机是 brief stage：当 `POST /api/review/iterate stage=brief` 返回 `manual_modes` 包含 `MISSING_DIMENSION` 时，才允许向用户发送补充问题。其余所有 revise/blocked 均由 Agent 自动重跑，用户不应看到任何审核细节。

#### 11.5 项目生命周期端点

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/project/create` | 创建项目，记录到 SQLite |
| GET | `/api/project/{id}/status` | 查看当前项目所有 stage 状态 |
| POST | `/api/project/{id}/finalize` | 检查全部 REQUIRED stage PASS → 返回 RELEASED |

> 调用任何下游操作（stitch/burn/finalize）时带上 `project_id` 字段，平台会自动检查上游 gate；不传 `project_id` 则保持原有行为（向后兼容）。
