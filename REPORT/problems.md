# 问题清单（按严重度排序）

严重度定义：**P0 阻断出片** / **P1 明显影响质量或账目** / **P2 工程与体验问题** / **P3 观察项**。

---

## P-01 (P0) VibeVoice 短句配音时长完全不可控——seed 随机性

**现象**：同一句 14 字旁白，不同 seed 生成的音频从 1.9s 到 15.7s 不等（实测 8 个 seed：
4.8 / 5.3 / 7.6 / 7.9 / 9.6 / 13.9 / 15.7 / 16.5s，坏种率约 75%）。
即使节点侧关掉 sampling（`use_sampling=False`）仍非确定——重复提交同一 seed 也得到不同时长
（seed 42 第一次 15.73s，第二次 5.33s）。音频内部无静音可裁（-70dB 阈值下 0.1s 以上
静音为 0），是连续的慢速语音， autocorrelation 无周期性（排除"重复念 N 遍"）。

**影响**：旁白长度直接决定 align 窗口；超长旁白要么被变速压到失真，要么把 3s 镜头
冻结延长到 5-6s，成片节奏被拖垮。这是本次唯一的 P0，也是本地配音链路的最大短板。

**根因**：VibeVoice-1.5B 的时长预测是逐 token 随机过程，短句尤其容易"拖腔"。
平台原本的 `local_tts()` 没有任何时长控制（上游 `local_media.py` 只下载不处理）。

**处置**：已修复（见 fixes.md F-01/F-02）：多次 seed 重试 + 静音裁剪 + 限幅 2.0x 变速，
并把最短的一次 take 作为兜底。仍未根治——见 suggestions.md S-03（换确定性引擎/加服务端时长约束）。

---

## P-02 (P0) `_trim_silence` / `_atempo_fit` 临时文件扩展名导致 ffmpeg 静默失败

**现象**：节点侧配音后处理补丁的两个 ffmpeg 调用全部 no-op——坏音频原样到达对齐门，
报「S01 声音 15.73s + 气口 0.25s 超过素材上限」。最初误判为"补丁没生效/进程跑了旧模块"。

**根因**：`tmp = path + '.trim'` / `path + '.fit'`，输出文件形如 `S01.mp3.fit`，
ffmpeg 无法从 `.fit` 扩展名推断封装格式，muxer 初始化失败（`Unable to choose an
output format`），`returncode != 0` 但代码不检查、文件从不替换。
一个扩展名 bug 让整套后处理静默失效，且没有任何日志。

**处置**：已修复（F-01）。教训写进 suggestions.md S-07：静默失败必须留痕。

---

## P-03 (P0) `pipeline_runner.py` 缺 `import os`，本地 BGM 路径必崩

**现象**：assemble 跑到混音步骤 500：
`NameError: name 'os' is not defined` at `pipeline_runner.py:1762`
（`if os.environ.get("SHIPIN_LOCAL_MUSIC", ...)`）。

**根因**：上游仓库在 assemble 里新增"本地音乐生成"分支时漏了 `import os`。
该分支只在 `bgm_path` 非空时执行到——纯 agnes 云端流程不生成 BGM 文件时
`bgm_path` 恒为 None 且整段被上游条件短路，所以这个 bug 在官方演示路径上永远不会暴露，
**只有启用本地媒体后端才会 100% 触发**。

**处置**：已修复（F-03）。

---

## P-04 (P0) BGM 闪避的 ffmpeg 滤镜图"消费了同一个输出标签两次"

**现象**：`master_audio()` 组装的 filter_complex 直接报
`Invalid stream specifier: ne0 ... matches no streams`，
`Error initializing complex filters`，assemble 无法混音。

**根因**：滤镜图里第一个旁白事件标签 `[ne0]` 同时被两处引用——
`[bgm0][ne0]sidechaincompress=...[bgm]`（闪避侧链）和最终
`[ne0][ne1]...amix`（混音输入）。ffmpeg 的滤镜图中**一个输出标签只能被一个输入消费**，
重复引用即整个 graph 解析失败。二分验证：把 `[ne0]` 改成只用一次即通过；
用 `asplit` 复制一份给侧链也通过（实测用例 M/R/S）。

**影响**：所有走 `narration_events + bgm + duck=True` 的混音路径全挂，
即"旁白分镜定位 + BGM 闪避"这个平台主打特性在本地后端下不可用。

**处置**：已修复（F-04）：闪避前对侧链源 `asplit=2`，amix 用改名后的副本。

---

## P-05 (P0) align 窗口延长语义在 assemble 落空：音画时间线劈叉 3.8s

**现象**：assemble 字幕验收失败：
`cue5: 字幕 cue #5 结束于 20.49s，超出视频时长 16.67s——该字幕不会显示`。
旁白时间线合计 20.49s，stitch 出来的画面只有 16.67s。

**根因**：align 阶段对"旁白比镜长长"的镜头计算延长窗口（`window = max(shot_dur, needed)`，
timeline 里还带 `extended` 标记），但 stitch 的补料路径**只认 manifest 里的 `master` 字段**
（一个更长的同镜素材）。本项目中所有镜头的 `master` 均为 `null`（master 只在 agnes
云端长素材协议下产生），于是补料从不发生：`_trim()` 对"源比目标短"只是原样拷贝，
画面停在原始 3s，旁白照样铺 20.49s。尾字幕整条溢出片尾。

**影响**：只要旁白总长 > 画面总长（本地短素材 + 长旁白就会这样），必然失败。
这不是边缘 case，是本地后端的常态。

**处置**：已修复（F-02 关联）：无 master 时冻结末帧补足窗口，并按 `source=freeze` 落账。
修后 stitch 20.5s ≈ 预期 20.49s，`boundary_preserved: true`。

---

## P-06 (P1) 成本账目把本地生成记成 agnes 云端价，虚报 $1.055

**现象**：`cost.json` 46 条记录里，17 条 `video/agnes-video` 各记 $0.06、
7 条 `image/agnes-image` 各记 $0.001——而这些全部是 DGX 本地出图出片（零现金成本）。
只有 TTS 诚实地记了 `$0`。

**影响**：C5 预算硬闸按这个账目扣额度；用户看到的"本片花了 $1.055"是幻觉。
对"全部本地生成"的部署场景，账目模型整体失真（虚拟币与真钱混记）。

**处置**：未改（属计价模型设计问题，见 suggestions.md S-01）。
建议本地后端记录 `provider: local` + 记 GPU 秒数而不是美元。

---

## P-07 (P1) 终审 VLM 对 3/5 镜有 critical 内容提示，成片未达 RELEASED

**现象**（本次实测的第一轮内容终审）：S01 杯中物在 t=1.14→1.91s 从咖啡纸杯突变为椰子
（`VLM_BREAK` + 2 条 `SHOT_STORY_MISMATCH`）；S02 关键帧人物穿针织开衫+围巾（御寒装）
与"闷热地铁车厢"场景矛盾，且正片窗外出现海景渡轮；S05 分镜要求"纯色背景产品静置"，
实际生成人物肖像。

**影响**：final review verdict=fix，`released=false`，成片停留在 candidate。
注意所有**确定性门（stitch 保时长/字幕/响度/时间线/旁白对齐）全过**，
卡住的只有 VLM 语义审查这一层。

**根因分类**：
- S01/S05 是 H3/zimage 对提示词的执行偏差（产品主体一致性弱）；
- S02 是**剧本自身矛盾**：LLM 写分镜时给了"冬季穿搭"的主体描述，场景却是酷暑地铁，
  平台没有跨字段一致性校验（主体 vs 场景的气候自洽）把这个矛盾拦在生成前。

**处置**：已按分镜做了一轮修复迭代（夏季服装、S05 改无人物产品静物、S01 静态机位+
显式"杯子保持纸杯"），重新生成后的复审结果见本报告 `verification.md` 末节。
跨字段一致性校验缺失写入 suggestions.md S-05。

---

## P-08 (P2) 平台没有分镜编辑 API，"改分镜"只能手改 JSON 文件

**现象**：要修 S02 的服装矛盾，翻遍 `api.py` 只有 `/api/pipeline/text`（从 brief 全量
重新生成 script+storyboard，不可控）和 `/api/review/iterate`（只做机械修复）。
没有 `PUT /api/projects/{id}/storyboard` 之类的受控更新端点。

**影响**：使用者（人或 agent）要么全量重生成接受 LLM 漂移，要么绕过平台直接改
`data/projects/*/storyboard.json`——后者会让 stage store 里记录的内容哈希与盘上文件脱节，
下游的"上游变更检测"形同虚设。本次实测就是走后门方式（如实记录）。

**处置**：未改（接口缺口，见 suggestions.md S-02）。

---

## P-09 (P2) 前端只在 `/ui` 挂载，根路径 404 容易误判"没部署"

**现象**：`curl http://host:7000/` 与 `/assets/...` 均 404，只有 `/ui/` 与
`/ui/assets/...` 200。第一次排查时误以为静态资源没部署，实际是挂载点设计如此
（`app.mount("/ui", StaticFiles(dist, html=True))`）。

**影响**：纯易用性/可发现性问题；文档却没写清楚入口路径，外部对接方容易误判。

**处置**：未改（见 suggestions.md S-06：根路径 302 到 /ui 或在 README 明示）。

---

## P-10 (P2) tarball 整体同步会静默回滚节点侧补丁

**现象**：开发期用 `tar czf` 整包上传同步代码，把节点上已打好的补丁
（local_media 健壮化、.env 参数）覆盖回上游原版，且无任何提示，
表现为"补丁明明打过又失效"的幽灵 regression，排查成本很高。

**影响**：工程协作陷阱，不是产品缺陷，但足以让下一个协作者踩坑。

**处置**：本次改用**单文件精确同步**（仅 assembly.py / pipeline_runner.py 两个改动文件），
未再整包覆盖。见 suggestions.md S-08。

---

## P-11 (P3) 主 ComfyUI 实例跑不了 VibeVoice（任务静默失败）

**现象**：h3api 负载均衡把 8 个并发 TTS 任务分给两个 ComfyUI 实例，
分到主实例（:8188，常驻 H3 大权重）的 3 个全部失败：
`no output files in comfy history (status=error)`，失败只体现为 job failed，
不带根因（主实例日志已滚掉，未能定位到具体异常）。

**影响**：TTS/音乐/生图任务有 ~40% 概率被路由到必然失败的实例，靠重试兜底
（浪费 40-60s/次），且并行试 seed 的策略实际不可用。

**处置**：已把 h3api 的实例路由改为「主实例只接视频模式，tts/音乐/生图只走小实例」
（fixes.md F-05）。根因未定位（见 suggestions.md S-04：补实例级错误透传）。

---

## P-12 (P3) 角色音色回退无提示地发生

**现象**：generate 报告 fallbacks：`S01..S05: role_code 'biz_female' 未在 cast_roles
配置——回落默认音色`。5 镜旁白全部静默回退默认音色。

**影响**：音色不可控（本次默认音色恰好可听，未造成事故），但用户以为指定了
"商务女声"实际没用上——配置与执行的静默偏差。

**处置**：未改（见 suggestions.md S-09：fallback 应显式告知并记录到 cost/manifest）。

---

## P-13 (P0) 首帧图缓存不讲新鲜度——分镜改了首帧永不重画

**现象**：连续 4 轮 generate（跨 1.5 小时、3 次 storyboard 重写：夏季服装/S01 改
产品微距/去掉椰子）里，`S01.jpg` 的 mtime 始终停在首次生成的 15:36，视频全部锚在
旧首帧上。表现为「改了什么都不生效」：S02 的 clip 一直是冬装、S01 的 clip 一直
有人物、椰子一直在画面里。

**根因**：`run_generate_phase` 的首帧循环只判断「manifest 里记的 first_frame 路径
存在 → 素材池命中，跳过生成」+「`{sid}.jpg` 不存在才生成」。没有像 clip 那样的
输入指纹（`_clip_input_sha`）新鲜度判据——分镜/提示词变更后首帧永不重生成，
而视频却会因 prompt 变化重生，于是新视频锚在旧图上。

**影响**：本地后端下这是最高危的缓存缺陷——它让「改分镜→重新生成」这个核心迭代
循环静默失效，且所有下游审查（QC/VLM）审的是新旧混合体，无门能发现。

**处置**：本次实测靠「手工删 jpg + 清 manifest 引用」绕过（已在报告中如实记录）。
根治建议见 suggestions.md S-10：首帧也要过内容指纹新鲜度。

---

## P-14 (P0) storyboard 的 dialogue 是字符串，TTS 合成只认 dict——台词音频永远不合成

**现象**：成片从头到尾只有旁白，角色台词（S02「这味道，绝了！」/S03「心静了。」）
既无音频也无字幕，而 brief 硬规则要求「全片至少 2 镜 dialogue」。查 h3api 任务历史，
平台从未为台词提交过 TTS 任务。

**根因**：STORYBOARD_PROMPT 模板把 dialogue 定义成纯字符串（"逐字复制剧本台词"），
而 SCRIPT_PROMPT 生成的是 `{role_code, text}` dict。generate 的 TTS 合成、align 的
dialogue_path、assemble 的声音设计三处都只认 dict——`isinstance(dlg, dict)` 一票否决，
字符串台词被静默跳过。纯云端演示路径同样中招（与后端无关，是数据形状劈叉）。

**处置**：已修复（F-08）：generate 加载分镜时把字符串台词归一为
`{role_code: "hero_male", text}` dict 并落盘，三处下游同时恢复。修复后
S02_dlg/S03_dlg 音频正常合成，align 的 voice_tail 也正确包含台词。

---

## P-15 (P1) manifest 只在 generate 成功时落盘——失败轮次留下「盘新账旧」，G5 误拦

**现象**：多轮 generate 中途失败后（某镜 QC 三连败即 return），新 clip/canvas 已写在
盘上，manifest 却还是上一次成功轮的旧 sha；随后 assemble 的 G5 素材哈希门以
「素材内容与 generate 阶段验收时不一致」硬拦，且**没有任何自愈路径**——
重跑 generate 若该镜缓存命中，缓存分支只信 manifest 旧记录、不校验盘上文件，
于是无限循环被拦。

**根因**：manifest 的唯一落盘点在 generate 成功末尾（align ok 之后）；
clip/canvas 却是每镜即时写盘；缓存命中分支不做哈希对账。

**处置**：已修复（F-09）：缓存命中时校验盘上 canvas 哈希与记录，不符则从同源 clip
确定性重派生 canvas 并刷新记录。修复后 S01 的劈叉自愈，assemble 放行。

---

## P-16 (P2) 否定式提示词把禁物拽进画面（T2V 模型特性×平台无提示）

**现象**：为压「生椰拿铁被画成真椰子」，在 motion 里写 "no hands, no people,
no coconut"——S03（海景空镜）反而从 1.1s 起漂成椰子+杯子特写；去掉椰子色调
（style anchor）后仍复现。实测：正面措辞（只描述要什么）的海景提示词才稳定。

**根因**：扩散/自回归视频模型对否定 token 的接地很弱，"no X" 常起强调作用。
平台的提示词模板与审查都没有提示使用者避免否定式描述。

**处置**：实测口径已改为纯正面措辞（见 fixes.md F-10）。模板层建议见 suggestions.md S-11。

---
