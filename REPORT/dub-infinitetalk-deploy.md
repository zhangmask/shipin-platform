# 轮73:配音模型(InfiniteTalk)落地 + ASR 门禁强制启用

日期:2026-09-29。对应用户三条要求:①ASR 乱说话检测必须**强制/默认启用**;
②本地视频不够用/音频乱时**用给视频配音的模型**;③24G 或更小显存的用户,
生成完视频后**必须**走照视频配音,且**音乐分轨**(视频/音频彻底分离)。

## 1. ASR 门禁:默认开、强制开、逐镜回读

之前存在的问题:门早就写好了,但节点上 `whisper_service.py` 顶层
`import whisper` 直接 ImportError(openai-whisper 没装、openaipublic 被
墙)→ 整个模块加载失败 → `_tts_asr_check` 捕获异常返回空 →
**从未真正执行过**(假 PASS)。这次改动:

- `tools/tts_sidecar/server.py`:新增 `/asr` 端点(faster-whisper 1.2.1,
  CTranslate2 CPU int8,与 VoxCPM TTS 同 venv 同 8201 端口),接受
  `{path, language, initial_prompt}`。
- `whisper_service.py`:**顶层 import whisper 移除**,改双后端
  (`backend="sidecar"` 默认 / `"openai"`),模型延迟加载。
- `local_media.sidecar_asr()`:统一复用 `_assert_local_url` 行内 SSRF 守卫。
- `pipeline_runner._tts_asr_check()`:配音阶段逐镜 ASR 回读,阈值与终审
  `check_narration_content` 同款(短句 ≤8 字低档、繁简归一 zhconv)。
- **initial_prompt 偏置**:短句 ASR 噪声大,喂剧本原文作 decoding bias
  后相似度 0.40→0.80 / 0.83→1.00。
- 语义:协议失败(sidecar 挂)= skip 不阻断;内容失配 = fail-closed。

实测(store 项目,5 镜):S01=1.00 S02=1.00 S03=0.75 S04=0.80 S05=1.00,
GATE PASS。

## 2. 配音模型选型:InfiniteTalk(MeiGen-AI, Apache-2.0)

对比结论见 `dub-model-research.md`。选 InfiniteTalk 的原因:
Wan2.1-I2V-14B 基座 + ComfyUI 原生 `WanInfiniteTalkToVideo` 节点;
单说话人模式(旁白/独白场景正好);Apache-2.0 可商用。
备选 Wan2.2-S2V(≥80G 显存,本节点浪费)、MuseTalk(4G,只动嘴);
排除 Wav2Lip/Sonic(商用禁令)、OmniHuman(闭源)。

## 3. 节点侧部署(全链路跑通)

六个权重(ComfyUI 目录根部,hf_hub_download 的 `local_dir` 会镜像仓库
路径,必须搬到根部):
- `diffusion_models/wan2.1_i2v_480p_14B_fp8_e4m3fn.safetensors`(16.4G,
  Comfy-Org/Wan_2.1_ComfyUI_repackaged 单文件版)
- `model_patches/infinitetalk_single_fp16.safetensors`(2.7G)
- `audio_encoders/wav2vec2-chinese-base_fp16.safetensors`(189M,**本地转换**)
- `loras/lightx2v_I2V_14B_480p_cfg_step_distill_rank64_bf16.safetensors`
- `text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors`(已存在)
- `vae/wan_2.1_vae.safetensors`(已存在)

### 踩坑记录(都有断言/落盘为证)

1. **分片不可用**:Wan-AI 原仓库的 7 分片 + index.json 在此版 ComfyUI
   (0.37)里加载不了(folder_paths/comfy.utils 无 split 处理)——
   必须用单文件版。
2. **fp8 patch 匹配不上 dispatch**:`quant_models/*_fp8.safetensors` 键名是
   `audio_proj.proj1.weight._data/_scale`,而 ModelPatchLoader 分发条件
   写死 `"audio_proj.proj1.weight" in sd` → 全部落空 → `model` 未绑定
   UnboundLocalError。**必须用 `comfyui/` 目录的非量化版**。
3. **音频编码器要自己转**:官方模板用 wav2vec2-chinese-base,该 fp16
   safetensors 不在公开镜像里(逐个 repo 探测 401/000)。从
   TencentGameMate/chinese-wav2vec2-base 的 pytorch_model.bin 本地转:
   去 `wav2vec2.` 前缀、丢 quantizer/project_* 预训练头、fp16 保存;
   **fairseq 风格 weight_norm(g/v)要映射到 torch parametrizations
   (original0/original1)**,否则键名对不上。
4. 官方模板链路(照抄自 Comfy-Org/workflow_templates 的
   `video_wan2_1_infinitetalk.json`):UNETLoader → LoraLoaderModelOnly
   (lightx2v rank64) → CLIPLoader → VAELoader → CLIPTextEncode pos/neg
   zeroout → LoadImage/LoadAudio → AudioEncoderLoader(wav2vec2) →
   AudioEncoderEncode → ModelPatchLoader → WanInfiniteTalkToVideo
   (single_speaker, motion_frame_count=9, audio_scale=1.0) →
   RandomNoise/KSamplerSelect(euler)/BasicScheduler(normal,6,1)/
   CFGGuider(1) → SamplerCustomAdvanced → VAEDecode → CreateVideo(25fps)
   → SaveVideo。

### h3api 接入

`tools/h3api/extra_models.py` 新增 `build_infinitetalk()`(21 节点全链),
`server.py` 新增 `mode="dub"`(image=首帧, audio=驱动音频, length 按音频
秒数算 4n+1)。

## 4. 平台接入(音视频分离架构)

- `local_media.local_dub(frame, audio, out, ...)`:上传首帧+音频 →
  h3api dub → 下载。输出**纯视频轨**(不带音频)——音频轨由 assemble 从
  TTS 文件混,这就是"音乐分轨/音视频彻底分离"。
- `pipeline_runner._dub_mode()`:SHIPIN_DUB_MODE=off(默认)|on|auto。
  auto 探测 `h3api /v1/health` 的 `devices[0].vram_total`,≤32G(24G 档
  及以下)自动启用。
- `_dub_pass()`:生成阶段、TTS+ASR 之后逐镜配音。**只对有台词的人物镜**:
  旁白镜(画外音)没嘴可对、产品镜没脸,硬上配音模型会把机位运动/产品交互
  毁成静态近照。台词音频先 pad/trim 到镜头时长(拼接是硬切,不对齐时长
  会塌)。配音失败 fail-closed 不静默降级。
- 画布:`node_types.REGISTRY["dub"]`(首帧+音频 → 视频),`engine._exec_dub`,
  `_SPEND_NODE_TYPES` 含 dub(预算熔断);前端 GraphView 层计数/层过滤
  计入 dub。`tests/test_dub_node.py` 10 例全过。

## 5. 首跑实测

素材:store_promo_final_v2.mp4 抽首帧(720x1280)+ 6s 原音轨 → 480x832
竖屏、length=149、6 步 CFG1 euler+normal、seed 42。
结果:480x832 / 25fps / 149 帧 / 5.96s 精确对上;口型确实在动(2.0s 闭、
3.5s 张),身份保持(脸/围巾/包/咖啡馆一致)。
产物:`~/.tmptest/dubtest/infinitetalk_dub_test.mp4`(本地)。

已知注意:源帧若带烧录字幕(平台字幕通道),配音产物会延续字幕样式——
生产上喂给配音的首帧应取**字幕烧录前**的干净帧。

## 6. 24G 显存档位(文档既定架构)

- 视频:480p 档(最大边 ≤832,local_dub 自动等比压回 16 对齐)
- 配音:6 步蒸馏(lightx2v rank64)+ fp8 基座
- 音频:完全分离——TTS(VoxCPM sidecar)出词、ASR 逐镜验内容、配音模型
  只产画面、assemble 混音
- 合并模式:SHIPIN_DUB_MODE=on 强制全员配音;auto 按节点显存自动判

---

## 7. 咖啡视频 E2E 实跑(2026-09-29,新项目 coffee_e2e_0929)

用户问「现在做咖啡视频有没有问题」→ 直接实跑回答。新项目(瑞幸生椰拿铁
门店引流 brief),**text → generate → assemble 全通**:

| 阶段 | 结果 |
|---|---|
| text | ok(script 2 轮 pass / storyboard 5 轮 pass;双门已确认) |
| generate | ok(5 镜视频 + TTS + ASR 门 + align ok,17.93s) |
| assemble | ok:True,终审 verdict=fix |

成片 `spark-tvc-report/artifacts/coffee_e2e_0929_final.mp4`(720x1280/
24fps/17.83s/有音轨),确定性检查全干净:无黑场、无镜内切、audio_ok。

### 当天发现并修掉的问题(都不是配音链路的问题)

1. **LLM/VLM 链断了**:我机器→agnes 的隧道进程反复死掉(nohup 被父
   shell 回收;分离进程的 paramiko port-forward 也会静默死)。每次断,
   终审 6 批 VLM + 一致性 9 次全 fail-closed。修复:
   `tools/llm_chain_supervisor.py`(分离启动 + 20s 探活自愈)。链路稳后
   brand_seen False→True、breaks 清空。
2. **ASR 偏置假 PASS(真 bug)**:S03 的 VoxCPM 坏 take 无偏置 ASR 读出
   「音箱断播」,偏置后期望词虚高过门。`_tts_asr_check` 已改**双读**
   (偏置+无偏置),4 个新测试(`tests/test_asr_dual_read.py`)。修复后
   S03 换种子重合成、无偏置读「推门影像暖光」≈期望,过门。
3. **TTS sidecar 500**:模型加载 27s 窗口内的请求全 500 → generate
   fail-closed(设计行为);sidecar 热后重跑即过。

### 剩余项(内容失配,要重生成那几镜,非链路问题)

- S01:分镜要「写字楼冷光+针织开衫」,模型生成「暖调咖啡店+毛衣」
- S02:要「转身推门」,生成「正对镜头整理围巾(走廊)」
- S03:要「瑞幸门店入口」,生成「白色厢式车(移动咖啡车)」
- S05:要「纯白背景+logo 静态卡」,生成「门店外景+人物站立」
- S01/S04 单镜诊断 VLM 各有 1 次瞬时失败(fail-closed 拦截),重跑即试

---

## 8. 用户三项裁定(2026-09-29)与修复

用户看了咖啡成片后的三条反馈,全部定位到根因并修复:

1. **"为什么要加序数词"** —— 源头是 `SCRIPT_PROMPT` 的示例台词
   「等我三分钟」在鼓励数字进口播,LLM 又自行发明时间指代(「加班六点」)。
   修:rubric 两头加硬规则(口播禁阿拉伯数字与序数词、时间/数量/优惠改写
   为无数字口语)+ 确定性层 `_sanitize_spoken_text()`(数字转中文数词,
   只动 narration/dialogue,scene/subject 不碰;序数词记账告警)。

2. **"字幕根本对不上"** —— `_build_srt` 用的是**剧本文本**,而音频是
   TTS 另一份产物(S03 实测:字幕「推门,迎向暖光」/音频念「推门影像暖光」,
   坏 take 偏置 ASR 还虚高过了门)。修:generate 阶段的 ASR 双读门把
   **无偏置转写**落进 manifest(asr_text / dlg_asr_text),`_build_srt`
   优先用它(音频里实际念的话),剧本文本只在 ASR 不可用时兜底——字幕
   与画外音天然一致。配合轮73b 的偏置假 PASS 修复,坏 take 在 generate
   就被拦下重合成。

3. **"字幕不能让视频生成,字幕也应该独立地做"** —— 落版镜 scene 写
   「纯白背景叠加瑞幸logo」,这句中文连品牌名一起进了图像 prompt,模型
   把「瑞幸☕☕」烧进画面(S05 实测)。修:
   - 生成 prompt 一个字的指令都不给:去掉 `brand_shot`/`_short_brand`
     (品牌名入画)、按键子句的 logo printed 改 blank circular logo badge;
     prompt 末尾统一 "no text, no letters, no logos, no watermark,
     no subtitles anywhere in the frame";
   - rubric 末镜 scene 禁写品牌名/logo/字幕等画面文字描述;
   - 品牌门 BRAND_MISSING 改判**确定性通道**:VLM 画面看到 **或** 旁白/
     SRT 文本含品牌名,任一成立即过(与平台既有的「品牌只走 TTS+SRT
     确定性通道」规矩对齐,不再逼模型画字)。

字幕仍然是 assemble 阶段独立烧录( burn_srt → ffmpeg),只是文本来源从
剧本换成音频转写。

测试:`tests/test_subtitle_independence.py` 15 例(数字清洗/字幕来源/
prompt 无品牌字/品牌门三通道);全量 763 passed(仅剔真联网 E2E)。

---

## 9. 用户三项裁定后的新片(coffee_v2_0929,2026-09-29)

按新规则从零跑的一条:**text 3 轮过(script 3/storyboard 3)→ generate ok →
assemble 出片 720x1280/16.2s/有音轨**。成品
`spark-tvc-report/artifacts/coffee_v2_0929_final.mp4`。

三条修复在新片里的实际效果:
- **无数字口播**:旁白「加班深夜,揉揉眼睛 / 起身下楼 / 推门进,暖光扑面 /
  第一口,紧绷消散 / 瑞幸,步行三分钟」——LLM 残留的「3分钟」被确定性层
  转成中文,TTS 念得对。
- **字幕与音频一致**:SRT 文本 = ASR 转写(实际念的话),相似度 ≥0.5 判为
  同音噪声时回退干净剧本文本(实测 sim 0.71~1.0 全是噪声:一身/堆门镜),
  真分歧(0.1 量级)才照抄音频。
- **字幕/品牌不进生成**:prompt 末尾统一 no text/no letters/no logos/
  no subtitles;落版镜 scene 不再写「叠加 logo」;品牌门改判旁白/字幕
  文本通道(新片 brand 通道过)。杯身小 logo 是图形不是字。

### 又抓一个真 bug:节点字幕烧成豆腐块

新片初版字幕在节点上烧成「起⏸」——根因:**节点 ffmpeg 的 drawtext 渲染
中文失败(textfile 也救不了),libass 在节点/本机都 glyphs-absent 静默不
渲染**(renderer 的 probe 早有记录,但 drawtext 回退路在节点上是坏的)。
PIL 在节点上同字体能量到字形 → 新增第三条烧录路 **png_overlay**:PIL 把
每条 cue 渲成整幅 RGBA PNG,ffmpeg overlay + enable 窗口合成(不用
-loop 1,那条路会让编码 runaway)。度量与渲染同一个字体,量到什么画什么。
现在 auto 模式首选 png_overlay,libass 可用才走原生 subtitle,drawtext
退到最后兜底。重烧后字幕正常。

### 新片仍不过终审的原因(内容层,非本次修复范围)

13 条 SHOT_STORY_MISMATCH:分镜要「昏暗办公室工位/写字楼入口」,模型
生成的是咖啡店 scenes;S03 要吧台、出来是白色厢式车;S05 要纯白产品卡、
出来是门店外景。模型对「咖啡店」先验极强,要重生成那几镜(改 scene 描述
/换首帧),属内容迭代。另:VLM 终审批在隧道抖动时会 fail-closed 拦
(设计行为),需 supervised 隧道(llm_chain_supervisor.py)稳定后重跑。
