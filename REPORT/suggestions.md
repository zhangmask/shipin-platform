# 建议（按优先级）

---

## S-01 成本账目区分"本地 / 云端"，本地记资源不记钱 (高)

现状：本地生成记 agnes 云端价（video $0.06/3s、image $0.001），虚报 $1.055/片（P-06）。
建议：
- `cost.json` 记录增加 `provider: local|agnes` 与 `gpu_sec` / `comfy_instance` 字段；
- 本地条目 `usd: 0`，另出 `resource_summary`（GPU 秒、模型名、实例）；
- 预算闸 C5 对本地/云端分别设阈值（或本地只告警不拦截）。
收益：预算闸重新变得可信，"这片子花了多少"有真实答案。

## S-02 补受控的分镜/剧本编辑端点 (高)

现状：改分镜只能全量重生成或手改 JSON（P-08）。建议：
- `PUT /api/projects/{id}/storyboard`（带 schema 校验 + 内容哈希版本化 + `caller`）；
- 保存即触发"下游失效"：image_prompt/video_prompt 重新派生并重审，
  generate 侧 clip 指纹自然失活（已有 `_clip_input_sha` 机制，接上即可）；
- UI 画布上分镜节点可直接编辑 params（画布已支持改参重跑，缺的是阶段 API 对齐）。
收益：修 S02 那类"剧本自相矛盾"不再需要后门操作，版本史完整。

## S-03 配音时长确定性方案 (高)

现状：VibeVoice 短句拖腔无解，只能重试+变速兜底（P-01）。建议按代价排序：
1. h3api 的 VibeVoice 工作流启用 `voice_speed_factor`（节点原生支持 0.8-1.2），
   在生成侧就提速，比事后 atempo 音质好；
2. 服务端加"期望时长"约束：生成后超预算即内部换 seed 重试（把当前平台侧逻辑下沉到
   h3api，所有调用方受益）；
3. 评估确定性引擎（如 Kokoro 中文声线 + speed 参数）作旁白默认，
   VibeVoice 留给"表演型"台词。本次未采用 Kokoro 的原因：目标节点未装对应
   ComfyUI 节点（GitHub 不可达），非质量结论。
   另建议补充 ASR 抽检（本次节点无 whisper/funasr 环境，坏 take 只能靠时长猜）。

## S-04 实例级错误透传 (中)

现状：ComfyUI 执行失败只透传 `no output files in comfy history`，根因埋在实例日志
（P-11）。建议：h3api 轮询 `/history` 时把 ComfyUI 的 `status.status_str` 与
stderr 摘要带回 job.error；实例连续失败 N 次自动摘除路由并告警。

## S-05 分镜跨字段一致性校验 (中)

现状：S02 "冬季穿搭 × 酷暑地铁"这种剧本级矛盾要等终审 VLM 才发现（P-07），
一张首帧图的成本已付出。建议在 storyboard 审查里加**确定性**规则：
主体描述与场景的气候/季节词表冲突检测（围巾/羽绒/针织开衫 vs 闷热/盛夏/酷暑）、
景别与运镜的合法组合表、每镜旁白字数 vs 时长（>4.5 字/s 报警）。
便宜的规则先拦，VLM 留给语义。

## S-06 部署可发现性 (低)

`/ui` 之外根路径 404（P-09）：根路径 302 到 `/ui/`，或在启动横幅与 README
首屏写出入口 URL。另建议 `start.sh` 支持 `--port` 缺省时打印完整访问地址。

## S-07 静默失败必须留痕 (低)

P-02 的教训：ffmpeg 后处理失败被完全吞掉。建议平台内所有
`subprocess.run(...) returncode != 0` 分支至少 `logger.warning` 并把原因写进
该步骤的产物 JSON（` stitch_result.json` 那样的结构），失败可见才可排查。

## S-08 部署协同：单文件同步 + 补丁即代码 (低)

整包 tar 同步回滚补丁（P-10）的根治：把节点侧补丁脚本纳入仓库
（如 `deploy/spark/patches/`），启动脚本"应用补丁 → 起服务"，幂等可重入。
本次已改为单文件同步，但补丁仍在个人工具目录里，未随仓库走。

## S-09 音色回落与首帧缓存都要显式 (中)

P-12（音色静默回落）与 P-13（首帧缓存不讲新鲜度）是同一类病：
**配置/输入变了，执行侧静默沿用旧值**。建议：
- 首帧/末帧与 clip 一样过输入指纹新鲜度（`_clip_input_sha` 同款机制），
  分镜或提示词变更即重生成，并在 generate 报告里逐镜标注 `frame_regenerated`；
- voice_fallback 除浮到响应外，写入 manifest 对应镜的 `voice` 字段与 cost note。

## S-10 视频提示词禁用否定式描述 (中)

P-16 实测：`no coconut / no people` 把禁物拽进画面（否定 token 起强调作用）。
建议 video_prompt 模板与审查加一条硬规则：motion/scene/subject 只允许正面描述；
确需排除的内容用「替换法」（把镜头交给别的主体）而不是否定词。
平台可在派生提示词时自动剥离常见否定模式并告警。

## S-11 反推"素材池命中"的适用边界 (低)

P-13 的缓存逻辑本意是变体复用基准素材池（跨项目共享）。建议把
「manifest 指向的文件存在就跳过」收紧为「存在 **且** 该镜属于池化素材
（`_clip_is_pooled` 同款判据）」，本地项目内素材一律过新鲜度。
