# 复现参数与产物清单

## 1. 环境与端点

| 项 | 值 |
|---|---|
| 节点 | `ssh -p 6023 asus_gx10@61.172.235.130`（密码另有保管） |
| 平台 | uvicorn `src.api:app` 0.0.0.0:7000，公网映射 7023 |
| 平台 UI | http://61.172.235.130:7023/ui/ |
| h3api | 127.0.0.1:9000（节点本机）；公网 9023 |
| ComfyUI 主实例 | 127.0.0.1:8188（MiniMax-H3，66GB 纯血 bf16） |
| ComfyUI 小实例 | 127.0.0.1:8189（zimage / VibeVoice / Music3） |
| LLM | agnes-3.0-flash，经 `127.0.0.1:8790` 反向隧道代理（Windows 侧 agnes_proxy.py + tunnel_supervisor.py） |
| 平台 venv | `~/envs/shipin`（Python 3.12） |
| ComfyUI venv | `~/envs/comfyui`（transformers==4.57.6，勿升 5.x） |

## 2. 本项目关键配置（节点 `~/shipin-platform/.env`）

```
SHIPIN_MEDIA_BACKEND=local
SHIPIN_LOCAL_IMAGE_ENGINE=zimage        # 720x1280 竖屏首帧
SHIPIN_LOCAL_VIDEO_ENGINE=h3            # MiniMax-H3 i2v，3s/镜（73 帧，%17==5 栅格）
SHIPIN_LOCAL_TTS_ENGINE=vibevoice
SHIPIN_LOCAL_TTS_MAX_SEC=2.9            # 每镜旁白预算
SHIPIN_LOCAL_MUSIC=1
SHIPIN_LOCAL_MUSIC_ENGINE=music3
AGNES_BASE_URL=http://127.0.0.1:8790/v1
SHIPIN_AUTH_MODE=off
```

## 3. 项目数据（tvc_luckin_01）

- 画幅 720×1280（抖音竖屏），5 镜 × 3s 分镜 + 旁白驱动窗口，align 后总时长 20.49s
- 链式首尾帧：S01 尾帧=S02 首帧；S02/S04 自末帧（own_end）
- 边界：cut / dissolve / cut / dissolve（无 master 时 dissolve 降级为 cut，stitch warnings 有记录）
- 旁白：5 句（每句 ≤14 字，brief 要求），VibeVoice 种子重试 + 静音裁剪 + ≤2.0x 变速
- BGM：本地 Music3（caption: "TVC background music, <mood>, instrumental, no vocals"），
  sidechain 闪避（threshold 0.02 / ratio 8），BGM 增益 -19dB，音效 4 个 whoosh
- 响度：EBU R128 两遍 loudnorm → **-14.2 LUFS**

## 4. 复现命令序列（节点侧）

```bash
# 阶段一（brief → script+storyboard，含 LLM 审片循环）
curl -s -X POST http://127.0.0.1:7000/api/pipeline/text \
  --json @brief_req.json          # {project_id, brief:{...}}
curl -s -X POST http://127.0.0.1:7000/api/project/confirm \
  -d '{"project_id":"tvc_luckin_01","gate":"script","approved_by":"user"}'
curl -s -X POST http://127.0.0.1:7000/api/project/confirm \
  -d '{"project_id":"tvc_luckin_01","gate":"storyboard","approved_by":"user"}'

# 阶段二（首帧 → 链式视频 → 逐镜 QC → TTS → 对齐）
curl -s -m 7200 -X POST http://127.0.0.1:7000/api/pipeline/generate \
  --json '{"project_id":"tvc_luckin_01"}'

# 阶段三（落版卡 → 转场拼接 → 调色 → 字幕 → 声音设计 → mux → 归一 → 终验）
curl -s -m 7200 -X POST http://127.0.0.1:7000/api/pipeline/assemble \
  --json '{"project_id":"tvc_luckin_01"}'
```

## 5. 产物

| 文件 | 说明 |
|---|---|
| `artifacts/final_tvc.mp4` | **最终成片**（22.3s，720×1280，h264+aac，双语音轨+字幕+本地BGM，-14.34 LUFS） |
| `artifacts/final_contact.jpg` | 最终成片抽帧拼图（每 28 帧取 1，5×3） |
| `artifacts/final_round1.mp4` | 第一轮成片（20.6s）——修复字幕溢出/闪避混音问题前的版本，留作对比 |
| `artifacts/contact_sheet.jpg` | 第一轮成片抽帧拼图 |
| `artifacts/new_frames.jpg` | 修正后重生的三张首帧（S01 产品微距/S02 夏季服装/S05 无人物产品） |
| `artifacts/s01_now.jpg` / `s03_now.jpg` / `s03_v2.jpg` | 排查用抽帧（S01 冰咖啡跳切、S03 海景漂杯子） |
| 节点 `.../tvc_luckin_01/final.mp4` | 最终成片原文件 |
| 节点 `.../tvc_luckin_01/manifest.json` | 每镜血缘 |
| 节点 `.../tvc_luckin_01/cost.json` | 成本账目（注意 P-06 的本地计价问题） |
| 节点 `.../tvc_luckin_01/versions/` | 分阶段版本史 |
| 图 `g-20260923224953-8ae092ec` | TVC 制作流程复刻画布（24 节点/22 连线），`/ui/` 图列表可打开 |

## 6. 工具脚本（本机 `D:\aishipin\tools\`）

| 脚本 | 用途 |
|---|---|
| `spark_ssh.py` | paramiko SSH 通道（connect/run） |
| `h3api/` | H3 视频/图/音乐/TTS 的 ComfyUI prompt 构建 + FastAPI 服务 |
| `agnes_proxy.py` / `tunnel_supervisor.py` | agnes API 反向隧道（Windows 侧常驻） |
| `patch_tts_fit.py` / `patch_tts_best.py` | 节点 TTS 补丁（F-01/F-02） |
| `sync_assembly.py` / `sync_runner.py` | 单文件精确同步（F-03/F-04） |
| `run_asm.py` / `rerun_gen.py` | tmux  detached 跑 assemble/generate |
| `build_graph.py` | 复刻图构建 |
| `dbg_*.py` | 本次排查用（seed 分布、静音结构、ffmpeg 二分） |
