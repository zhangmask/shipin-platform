# 视频配音 / 口型驱动（Audio-Driven Video / Lip-Sync）模型调研报告

> 目标场景：**已有视频 + 音频 → 重新生成口型 / 人物动作**（dubbing / talking-head re-animation）
> 部署目标：**DGX Spark 节点（aarch64 GB10，128GB 统一内存，约 130GB 可用）**，同时兼容 **24GB 单卡小显存用户**
> 调研日期：2026-09-27（信息以各仓库/官网当日页面为准）

---

## 0. 结论速览（TL;DR）

| 档位 | 结论 |
|---|---|
| **主推（质量+可部署性综合最优）** | **InfiniteTalk**（MeiGen-AI，Apache-2.0，Wan2.1-14B 基座，原生支持 音频+视频→视频 V2V 稀疏帧配音、无限时长、480p/720p、ComfyUI 官方节点、ModelScope 权重） |
| **备选 1（画质/电影感最强）** | **Wan2.2-S2V-14B**（阿里，Apache-2.0，原生 ComfyUI 节点，内置 CosyVoice TTS 一体，ModelScope 权重；官方标称需 80GB，DGX 富余、24G 需 GGUF/fp8+offload） |
| **备选 2（真·局部重绘 + 商用安全）** | **MuseTalk v1.5**（腾讯音乐，MIT 明确可商用，只重绘 256×256 面部区域、保留躯干/背景，实时级，4GB 起） |
| 24G 多说话人对白场景备选 | **HunyuanVideo-Avatar**（fp8+TeaCache 10–24GB） |
| 轻量图生视频备选 | **EchoMimicV3 Flash**（12GB，768×768，Apache-2.0） |
| 明确排除 | Wav2Lip（禁商用）、Sonic（非商用）、OmniHuman（未开源）、Avatar V / JoyStreamer（仅论文） |

---

## 1. 需求拆解

「已有视频 + 音频 → 重生成口型/人物动作」在开源侧实际分三类接口，选型前必须先明确：

| 类型 | 接口 | 能否 1:1 重驱动已有说话视频 | 保留原躯干/背景（局部重绘） | 代表模型 |
|---|---|---|---|---|
| **A. V2V 配音（video+audio→video）** | 整段说话视频 + 新音频 | ✅ 原生支持 | ⚠️ 整帧重生成，身份/背景近似保留，相机运动为"模仿非复制" | **InfiniteTalk**（主力）、Chanjing-Avatar-V2V-5B（试验性） |
| **B. 全帧唇同步（video+audio→video）** | 整段视频 + 新音频 | ✅ 原生支持 | ❌ 整帧重绘 | LatentSync、VideoRetalking（禁商用） |
| **C. 嘴部/面部局部重绘（mouth-region inpaint）** | 视频 + 新音频 | ✅ | ✅ 只改嘴/脸，其余像素不动 | **MuseTalk**、Wav2Lip（禁商用） |
| **D. 图+音频驱动（I2V，非重驱动）** | 首帧图 + 音频 | ❌ 需先抽首帧，原视频躯干/背景丢失 | ❌ | Wan2.2-S2V、HunyuanVideo-Avatar、Hallo2/3、EchoMimicV2/V3、Sonic、FantasyTalking、StableAvatar、MultiTalk、AniPortrait |

**关键判断**：若产品定义是"给已拍好的 TVC/口播视频换配音并改口型"，则 **A 类（InfiniteTalk V2V）是唯一成熟开源主力**；C 类（MuseTalk）作为"只许动嘴"的合规/快速档；D 类（Wan-S2V 等）适合"从参考帧重新生成"的新镜头而非重驱动。

---

## 2. DGX Spark（GB10 / aarch64）部署可行性（先决条件）

| 项目 | 事实（来源） |
|---|---|
| 硬件 | GB10 Grace Blackwell（sm_121/CUDA 13.x），128GB LPDDR5x 统一内存、**256-bit、273 GB/s 带宽**，GPU TDP 140W，芯片 AI 算力标称 1 PFLOP FP4（稀疏）。算力约独显 4090 级，**带宽明显低于独显** → 14B 级 DiT 属带宽瓶颈，必须配步数蒸馏 |
| 系统栈 | DGX OS（Linux）；NVIDIA 官方 ComfyUI Playbook 用 **cu130 PyTorch**（`pip3 install torch torchvision --index-url https://download.pytorch.org/whl/cu130`）或 NGC PyTorch 容器；PyTorch 可识别 GB10(sm_121) |
| ⚠️ 生态坑（官方 Playbook 原文） | 直装 Wan2GP requirements 会解析到 x86 cu12 轮子；**SageAttention / FlashAttention / xformers / decord 无 ARM 轮子**；torchcodec/onnxruntime-gpu nightly 轮子会漂移；transformers 5.x 的 higgs_audio_v2_tokenizer 与 Wan2GP 捆绑 TTS 冲突（需锁 transformers 4.54.0） |
| 对策 | attention 一律用 **sdpa**（ComfyUI 内 `--use-split-cross-attention`/attention_mode=sdpa，不要装 SageAttention）；ComfyUI 与 LLM/编码服务**不可共存**，跑视频前先停掉同节点上的大模型服务（统一内存池） |
| 显存策略 | NVIDIA Playbook：Tier1 视频工作流峰值约 80GB（Wan 720p），统一内存平台建议从 Tier1 起步、按压力降分辨率/帧数；130GB 下可放心 bf16 跑 14B，无需量化 |
| 国产/Arm 适配参考 | hao-ai-lab/FastVideo 已有 GB10(sm_121) 单卡 perf 配置；Wan2GP 官方 NVIDIA Playbook 已收录（`--profile 3` 为官方验证的 Spark 调优档），Wan2GP 内置 InfiniteTalk/MultiTalk 低显存支持 |

---

## 3. 全候选对比表

图例：接口 A=V2V 音频+视频→视频，B=全帧唇同步 video+audio→video，C=嘴部局部重绘，D=图+音频→视频。
显存为**推理**最低档（fp16 口径，除非注明）。

| 模型（机构/时间） | 接口/能否1:1重驱动 | 显存需求 | 输出质量 | 许可（商用） | ComfyUI | 权重渠道（国内可达） | 推理速度 | 多说话人 |
|---|---|---|---|---|---|---|---|---|
| **InfiniteTalk**（MeiGen-AI, 2025-08, arXiv 2508.14033） | **A**：音频+视频→视频，稀疏帧配音，**无限时长**；也可 I2V | 官方"very low VRAM"模式；FP8 权重存在；实测 **32GB 稳跑**（Q8 GGUF + block_swap20）、社区 **8GB 可跑**（fp8+35块交换+480²） | 唇同步优于 MultiTalk，头/身体/表情同步，**手/身体畸变少于 MultiTalk**；480p/720p；V2V 相机运动"模仿非复制"，SDEdit 可提升但有偏色 | **Apache-2.0** | **官方 comfyui 分支 + kijai WanVideoWrapper + ComfyUI 原生节点 `WanInfiniteTalkToVideo`** | HF（Wan-AI/Wan2.1-I2V-14B-480P + MeiGen-AI/InfiniteTalk）+ **ModelScope `MeiGen-AI/InfiniteTalk`**（apache-2.0，1.4万下载） | 40步基线；lightx2v **4步**蒸馏、TeaCache/APG 加速 | ✅ multi checkpoint / 多图工作流 |
| **Wan2.2-S2V-14B**（阿里, 2025-08, arXiv 2508.18621） | **D**：图+音频（+可选 pose_video）→视频；**内置 CosyVoice TTS 一体**（--enable_tts 文案直出视频） | 官方单卡命令注明 **≥80GB**（带 offload+dtype 转换）；社区 ComfyUI fp8_scaled/GGUF + offload 可压到 24G 级 | "电影感"最强，480p/720p，时长随音频 | **Apache-2.0** | **ComfyUI 原生节点 `WanSoundImageToVideo` / `WanSoundImageToVideoExtend`** | HF + **ModelScope `Wan-AI/Wan2.2-S2V-14B`**（apache-2.0，42.7万下载） | 需蒸馏 LoRA/TeaCache 才有可用速度 | 未明确（画面可多人，音频绑定主说话人） |
| **MuseTalk v1.5**（腾讯音乐 TMElyralab, 2025-03） | **C**：视频/图+音频 → **仅 256×256 面部区域 latent inpainting，躯干/背景像素不动**；bbox_shift 调嘴部开合 | 官方实测 **RTX 3050Ti 4GB fp16**（8秒约5分钟）；4060/8GB 125帧约数分钟；fp16 batch4 25fps | 256 面部区域 → 高分辨率下嘴部略糊，需外挂超分；表情/头部自然度中等，正脸最佳 | **MIT，代码+权重均明文允许商用** | `chaojie/ComfyUI-MuseTalk`（301★）、`kijai/ComfyUI-MuseTalk-KJ`、AIFSH 版 | HF `TMElyralab/MuseTalk`（官方仅 HF；国内用 hf-mirror） | **V100 30fps+，实时级**（有 realtime_inference 脚本） | ❌ 单人（可逐脸跑） |
| **HunyuanVideo-Avatar**（腾讯混元, 2025） | **D**：图+音频（多角色对白，FAA 逐角色音频注入） | 704×768×129 最低 **24GB（很慢）**；TeaCache 单卡可压到 ~10GB；fp8 checkpoint；有 CPU offload | 13B 混元基座，表情/情绪可控，多角色对话是亮点 | **Tencent Hunyuan 社区许可**：可分销/商用，但**限 Territory（不含 EU/UK/KR）**，>1亿 MAU 需另签 | 社区 `Yuan-ManX/ComfyUI-HunyuanVideo-Avatar`、`smthemex`版（VRAM>24G） | HF `tencent/HunyuanVideo-Avatar` + **ModelScope `Tencent-Hunyuan/HunyuanVideo-Avatar`** | 无官方数据；24GB 档"很慢" | ✅ 核心卖点 |
| **EchoMimicV3 Flash**（蚂蚁, AAAI 2026） | **D**：图+音频（+mask/prompt），免 mask；tasks 覆盖 portrait/body animation | **12GB**（Flash，partial_video_length 81/65 可再降） | 768×768，8 步生成（talking head 5步），Wan2.1-Fun-1.3B 基座 | **Apache-2.0**（明示不主张生成内容权利） | 社区 `smthemex/ComfyUI_EchoMimic`（16G 口径） | HF + **ModelScope `BadToBest/EchoMimicV3`**（apache-2.0） | A100 加速版 ~50s/120帧（9×加速） | ❌ |
| EchoMimicV2（蚂蚁, CVPR 2025） | **D**：图+音频+pose（pose 取自驱动视频）→ 半身 | 实测 V100 16G / 4090D 24G / A100 80G | 半身带动手；需外部 pose 对齐 | Apache-2.0 | 同上 ComfyUI_EchoMimic | HF+ModelScope `BadToBest/EchoMimicV2` | A100 ~50s/120帧（加速后） | ❌ |
| **LatentSync 1.6**（字节, 2025-06） | **B**：视频+音频 → 全帧唇同步（mask 帧拼接条件，非局部） | 最低 **18GB（v1.6）/ 8GB（v1.5）** | v1.6 训练于 512×512 缓解模糊；中文视频表现好；不稳手/非说话区 | **Apache-2.0** | `ShmuelRonen/ComfyUI-LatentSyncWrapper`（963★） | HF `ByteDance/LatentSync-1.6`（**无 ModelScope**，国内 hf-mirror） | 4060/8GB 125帧约 4分45秒（1.5, 256 internal, 20步, DeepCache） | ❌ 单人 |
| MultiTalk（MeiGen-AI, 2025） | **D**：图+多流音频 | 480p 单 **RTX 4090** 可跑（`--num_persistent_param_in_dit 0`）；Wan2GP 版 8GB 级 | 480p&720p（720p 需多卡）；≤15s（81帧/25fps 训练域，可至201帧） | Apache-2.0 | kijai WanVideoWrapper multitalk 分支 | HF（基座 Wan2.1 在 ModelScope；condition 权重仅 HF） | TeaCache 2–3×；FusioniX/LightX2V LoRA 4–8步；INT8+SageAttn 2-NFE | ✅ 多说话人对白 |
| FantasyTalking（高德, ACM MM 2025） | **D**：图+音频 | 0 常驻 DiT ~5G / 7B 常驻 ~20G / 不限 ~40G | 512×512×81 基准；Wan2.1-I2V-14B-720P 基座 | Apache-2.0 | 已并入 **kijai ComfyUI-WanVideoWrapper** | HF + **ModelScope**（均提供） | A100 15.5 / 32.8 / 42.6 s/it（40G/20G/5G 档） | ❌ |
| StableAvatar（NeurIPS 2026） | **D**：图+音频，**无限时长滑窗** | ~18GB（5s/480×832/4090）；顺序 CPU offload ~3GB；训练 50GB | 512²/480×832/832×480（改 dataloader 可 720p）；理论上可到小时级 | **MIT** | `smthemex/ComfyUI_StableAvatar`（10步，3×快） | HF `FrancisRing/StableAvatar`（无 ModelScope，hf-mirror） | 10步 ComfyUI 版 | ❌ |
| Hallo2（复旦, ICLR 2025） | **D**：图+音频（英文） | 未标注（SD1.5+AnimateDiff 级，约 15–25G 社区口径） | 长时长（展示 4K/最长1小时，靠 CodeFormer SR），正脸约束 | 仓库标 MIT；SR 部分涉 CodeFormer（S-Lab 1.0） | `smthemex/ComfyUI_Hallo2` | HF `fudan-generative-ai/hallo2` | 慢（SD1.5 级） | ❌ |
| Hallo3（复旦, CVPR 2025） | **D**：图+音频（**仅英文**） | 未标注（CogVideoX-5B-I2V 基座，约 24G+ 级） | 视频 DiT，动态更好 | **继承 CogVideoX-5B 许可**（可分商用需遵守其条款） | 无正式节点 | HF | — | ❌ |
| AniPortrait（浙大, 2024） | **D**：图+音频/pose；有 vid2vid 重演模式 | 未标注 | 512×512，中规中矩 | Apache-2.0 | `chaojie/ComfyUI-AniPortrait`、`frankchieng` 版 | HF `ZJYang/AniPortrait` + 无离子（wisemodel.cn） | 有 film 插帧加速 | ❌ |
| Sonic（腾讯+浙大, CVPR 2025） | **D**：图+音频 | 32GB 实测 | 全局音频感知，表情丰富 | **非商用**（商用须走腾讯云 VCLM） | `smthemex/ComfyUI_Sonic` | HF + Google Drive | 无 | ❌ |
| LongCat-Video-Avatar(-1.5)（美团, 2025-12 / 2026-05） | **D**：图+音频(+文本)、视频续写 | v1.5 有 INT8 DiT 开关 | Whisper-Large 音频编码（v1.5）、8步蒸馏、动画/动物泛化；480p/720p | **MIT（权重）** | 无官方节点（CacheDiT 加速） | HF + **ModelScope `meituan-longcat/LongCat-Video-Avatar-1.5`** | 8步蒸馏 | ✅ 单/多流音频 |
| OmniHuman-1 / 1.5（字节, 2025） | 1: 图+运动信号（音频/视频/两者） | 官网明示**不提供任何下载/服务** | SOTA 级（业界基准） | 闭源（论文/API 象限） | ❌ | ❌（只有第三方 Wan 复刻玩具） | — | 1.5 支持多人/非人 |
| Avatar V（arXiv 2606.13872, 2026-06） | 参考视频全 token 序列条件，1080p 无限时长 | 未公布 | 声称超 Seedance2.0/Kling O3/Veo3.1/OmniHuman1.5 | **仅论文，无代码权重** | ❌ | ❌ | — | — |
| Wav2Lip（Sync Labs/IITB） | **C**：视频+音频，脸 bbox 内重绘 | 极低 | 低（HD 模型 192×288 商用闭源） | **严格禁商用** | 社区零散节点 | 公开 | — | ❌ |
| VideoRetalking（OpenTalker） | **C/B**：嘴部+脸重绘 | 低 | 中 | 非商用研究许可 | ❌ 无 | — | — | ❌ |

---

## 4. 选型结论与部署方案

### 4.1 主推：InfiniteTalk（音频+视频 → 视频，V2V 配音）

**理由**
1. 唯一在接口语义上原生命中"整段说话视频 1:1 重驱动口型"的主流开源模型（sparse-frame V2V + 无限时长），并同时保留 I2V 模式（图+音频）。
2. Apache-2.0，权重 HF + ModelScope 双渠道（基座 Wan2.1-I2V-14B-480P 也在 ModelScope，国内拉全链路无阻断）。
3. ComfyUI 支持最全：官方 comfyui 分支、kijai WanVideoWrapper、**ComfyUI 原生 `WanInfiniteTalkToVideo` 节点**（ComfyUI v0.37 主干已含）。
4. 团队活跃：MeiGen-AI 2025-12 又开源 LongCat-Video-Avatar，1:1 重驱动路线持续演进。
5. 质量侧：唇同步优于 MultiTalk，手/身体畸变更少。

**已知限制**：V2V 是整帧重生成，原背景/相机为"近似复刻"（官方：相机运动并非逐帧一致，SDEdit 可提升但引入偏色，适合短片段）；想要"像素级不动背景"必须叠加 C 类方案或后期合成。

**方案 A-130GB（DGX Spark，GB10）**
```
软件栈：DGX OS + cu130 PyTorch（download.pytorch.org/whl/cu130）或 NGC PyTorch 容器 + ComfyUI（NVIDIA playbook-comfyui）
推理：InfiniteTalk single/multi cloth bf16 或 fp8 权重 + Wan2.1-I2V-14B-480P bf16
attention：sdpa（禁装 SageAttention/FlashAttention/xformers —— 无 ARM 轮子）
参数：480p 起步；可上 720p（带宽 273GB/s 下优先保步数）
步数：蒸馏 LoRA 4–8 步（lightx2v/FusioniX）+ TeaCache（0.25–0.30 系数档）
时长：单次生成 240–480s（V2V 无限时长滑窗，按音频切段）
量化：bf16（130GB 富余，无需量化；若与 LLM 共存则 fp8）
注意：跑前停掉同节点 LLM/编码服务（统一内存池）；transformers 锁 4.54.0 避开 Wan2GP TTS 冲突
预期：140W TDP + 273GB/s 带宽 ⇒ 14B 模型每步权重读取约 0.1s 量级，4-8 步 480p 5s 片段约分钟级
```
**方案 A-24GB（RTX 4090/3090，面向 24GB 小显存用户）**（boat2moon 实测 32GB 稳跑参数下调）
```
ComfyUI + kijai WanVideoWrapper
模型：wan2.1-i2v-14b-480p-Q8_0.gguf（17GB）+ Wan2_1-InfiniteTalk_Single_Q8.gguf（2.5GB）
base_precision=bf16；quantization=disabled（GGUF 已量化）；merge_loras=False
LoRA：lightx2v step distill strength=1.0（4 步）
block_swap=20（32GB 档）→ 24GB 改 30–35 并卸载 img/txt embeds
sampler=unipc，4 steps，cfg=1.0，shift=11.0
frame_window_size=81，motion_frame=9，audio_scale=1.5
CLIP：双图 concat（参考帧 + 人脸特写），tiles=4，ratio=0.5
分辨率：464×832 或 624×624 稳定；720×1280 慢；1080×1920 OOM（24GB 下上限按 720×1280 控制时长）
时长上限：单段建议 ≤ 120s（滑窗拼接）
```

### 4.2 备选 1：Wan2.2-S2V-14B（画质/电影感档，内置 TTS 一体）

- 适合"文案 → TTS(CosyVoice) → 口型视频"一体化新镜头生成，原生 ComfyUI 节点、ModelScope 42.7万下载、Apache-2.0。
- **方案 B-130GB（DGX Spark）**：bf16 直接跑（官方单卡命令即 ≥80GB 档），480p/720p，`--offload_model True --convert_model_dtype`；可选 `--pose_video` 做姿态驱动；长度随音频。
- **方案 B-24GB**：官方口径不可行（80GB），需 ComfyUI fp8_scaled / GGUF(Q8→Q5) + 强 offload + 4 步蒸馏，画质与稳定性有折损，**仅作试验档**；24GB 用户若要同类画质优先 InfiniteTalk。
- 局限：接口是"图+音频"，做 1:1 重驱动会丢失原视频躯干/背景；多说话人音频绑定未明确。

### 4.3 备选 2：MuseTalk v1.5（真·局部重绘 + 商用最安全 + 实时）

- 唯一"只重渲染嘴部、躯干/背景像素不动"的商用友好开源方案（MIT 明文含权重可商用）；256×256 面部 latent inpainting，V100 30fps+ 实时，3050Ti 4GB 可跑。
- **方案 C-130GB（DGX Spark）**：ComfyUI-MuseTalk 节点，fp16 batch 4–8、25fps，直接处理整段长视频（分钟级口播），后接 VAE/超分（可选 GFPGAN 类）提升嘴部清晰度；适合"只许动嘴"的合规场景或作为 InfiniteTalk 的快速草稿档。
- **方案 C-24GB**：fp16 batch 4、25fps，1080p 输入做区域处理（4060/8GB 实测 125 帧约数分钟）；时长上限按 CPU 预处理（人脸检测/裁剪）吞吐决定，建议单次 ≤ 60s。
- 局限：256 面部区域导致高清输出嘴部发糊；强侧脸/大角度姿态失败率上升；单说话人。

### 4.4 其他场景备选（24GB 单卡）

| 场景 | 选择 | 关键参数 |
|---|---|---|
| 多说话人对白配音 | **HunyuanVideo-Avatar** | 704×768×129，fp8 + TeaCache，10–24GB；注意腾讯社区许可的 Territory 限制（不含 EU/UK/KR） |
| 轻量图生视频数字人 | **EchoMimicV3 Flash** | 768×768，12GB，talking-head 5 步；Apache-2.0；ModelScope 可达 |
| 无限时长图+音频 | **StableAvatar**（MIT） | 1.3B 档 18GB / CPU offload 3GB；无 ModelScope，用 hf-mirror |
| 低显存极限（8GB） | InfiniteTalk Q8 GGUF + fp8 + 35 块交换 + 480×480（douyao 工作流已验证） | 8GB + 16GB 内存，页面文件 ≥30GB |
| 强唇同步简单替换 | **LatentSync 1.6**（Apache-2.0，ComfyUI 963★ 节点） | 18GB（1.6）/8GB（1.5），全帧重绘，无 ModelScope |

---

## 5. 汇总推荐矩阵

| 用户/节点 | 主用模型 | 配置 | 分辨率 | 步数 | 单段时长上限 |
|---|---|---|---|---|---|
| **DGX Spark 130GB** | InfiniteTalk V2V | 14B bf16，sdpa，block_swap 0 | 480p（可试 720p） | 4–8（蒸馏） | 240–480s |
| DGX Spark 130GB | Wan2.2-S2V-14B（画质档） | bf16 + offload | 480p/720p | 默认+TeaCache | 随音频 |
| DGX Spark 130GB | MuseTalk（合规/快档） | fp16 batch 4–8 | 原分辨率区域处理 | 单步 latent | 分钟级 |
| **24GB 单卡** | InfiniteTalk V2V | I2V Q8 GGUF 17G + IT Q8 2.5G，block_swap 20–35 | 464×832 / 624×624 | 4（lightx2v） | ≤120s |
| 24GB 单卡 | MuseTalk v1.5 | fp16 batch 4 | 原片区域 | 1 | ≤60s |
| 24GB 单卡 | HunyuanVideo-Avatar（多说话人） | fp8 + TeaCache | 704×768 | TeaCache | ~5s×N |
| 8GB 极限 | InfiniteTalk Q8 GGUF（douyao） | fp8 + 35 块交换 | 480×480 | 4 | 短口播 |

---

## 6. 风险与后续动作

1. **相机/背景保真**：InfiniteTalk V2V 对相机运动是"模仿"，长镜头可能漂移；若产品要求背景像素级不动，需 MuseTalk 局部重绘或后期合成（mask 融合）路线。
2. **GB10 生态**：SageAttention/FlashAttention/xformers 无 ARM 轮子，任何工作流都要预置 sdpa 降级；decord 在 Spark 上需替换（torchcodec 轮子漂移风险）。
3. **许可合规**：HunyuanVideo-Avatar 有 Territory 限制；Sonic/Wav2Lip/VideoRetalking 禁商用；OmniHuman 闭源无权重。
4. **观察名单（2026 新模型）**：Avatar V（1080p 无限时长、视频参考，仅论文）、JoyStreamer、LongCat-Video-Avatar-1.5（MIT + Whisper 编码 + 8步蒸馏，值得下季度复测）、Chanjing-Avatar-V2V-5B（Wan2.2-TI2V-5B 上的 V2V，尚早期）。
5. **建议下一步**：在 Spark 上用 NVIDIA ComfyUI Playbook（cu130 + Tier1）拉通 InfiniteTalk 480p 全链路，再用 `--profile 3` 与 sdpa 对比 s/it；24GB 侧按 4.3 参数表在 4090 上做 A/B（Q8 GGUF vs fp8_scaled）。

---

## 附：主要来源

- github.com/MeiGen-AI/InfiniteTalk、github.com/MeiGen-AI/MultiTalk
- github.com/Wan-Video/Wan2.2、huggingface.co/Wan-AI/Wan2.2-S2V-14B、modelscope.cn/models/Wan-AI/Wan2.2-S2V-14B
- github.com/TMElyralab/MuseTalk、github.com/chaojie/ComfyUI-MuseTalk
- github.com/Tencent-Hunyuan/HunyuanVideo-Avatar（LICENSE: Tencent Hunyuan Community License）
- github.com/antgroup/echomimic_v3、echomimic_v2
- github.com/bytedance/LatentSync、github.com/ShmuelRonen/ComfyUI-LatentSyncWrapper
- github.com/fudan-generative-vision/hallo2、hallo3；github.com/Tencent/Sonic(jixiaozhong/Sonic)；github.com/Zejun-Yang/AniPortrait
- github.com/Fantasy-AMAP/fantasy-talking；github.com/Francis-Rings/StableAvatar；github.com/meituan-longcat/LongCat-Video
- github.com/comfyanonymous/ComfyUI（comfy_extras/nodes_wan.py：WanSoundImageToVideo / WanInfiniteTalkToVideo）
- github.com/NVIDIA/dgx-spark-playbooks（playbook-comfyui、Wan2GP PR#100）；nvidia.com DGX Spark 产品页
- github.com/kijai/ComfyUI-WanVideoWrapper；github.com/boat2moon/infinitetalk-comfyui-workflow；github.com/H7ang0/douyao；github.com/zyli6269-cmyk/short-drama-4060-lipsync
- omnihuman-lab.github.io；arxiv.org/abs/2508.19209（OmniHuman-1.5）、2606.13872（Avatar V）、2508.18621（Wan-S2V）、2508.14033（InfiniteTalk）
