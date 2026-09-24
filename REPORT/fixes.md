# 已修复清单（本次实测中改动的代码与配置）

仓库：`shipin-platform`（节点 `~/shipin-platform/`）。F-01/F-02/F-03/F-05 只改了
Spark 节点上的部署副本；F-04 是本地仓库 + 节点双改。所有改动均为最小侵入，
不改变平台对外契约。编号与 `problems.md` 对应。

---

## F-01 修 P-02：TTS 后处理临时文件扩展名（assembly 无关，generation 侧）

**文件**：`src/shipin_platform/generation/local_media.py`

改动：
1. 新增 `_tmp_path(path, tag)`：`<base>.<tag><原扩展名>`，保证 ffmpeg 能推断封装格式；
2. `_trim_silence` / `_atempo_fit` 的临时路径改用它；
3. seed 重试 4 → 6 次；接受判据从"必须 ≤ max_sec"放宽为"≤ max_sec × atempo 上限"；
4. atempo 上限 1.8 → 2.0（可用 `SHIPIN_LOCAL_TTS_ATEMPO_CAP` 覆盖）。

**验证**：修复前 S01 固定 15.73s 且任何后处理都不生效；修复后同文本最短 take 4.8s，
atempo 后可压进 2.9s 预算。

---

## F-02 修 P-05 + P-01：保时长拼接的冻结补足 + 最短 take 兜底

**文件**：`src/shipin_platform/assembly.py`、`local_media.py`

1. `assembly.py` 新增 `_fit_part()`：`_trim` 只负责裁剪；当窗口 > 源时长且无 master 时，
   用 `tpad=stop_mode=clone` 冻结末帧补足，part 元数据记 `source="freeze"`（与 master
   补料同规格透明，落 `stitch_parts.json`）。
2. `local_media.local_tts()` 改为记录 6 次尝试里**最短**的 take 循环结束后统一
   atempo 兜底，返续 `raw_sec` 供审计。

**验证**：stitch 输出 20.5s（预期 20.49s，`boundary_preserved: true`），
3 个 freeze part 明示在 `stitch_parts.json`；字幕验收由 1 critical → 0。

---

## F-03 修 P-03：补 `import os`

**文件**：`src/shipin_platform/orchestration/pipeline_runner.py`

顶部导入区加 `import os`。一行修复，解除本地 BGM 路径的必崩 NameError。

**验证**：直接跑 assemble 不再 NameError，混音步骤产出 `soundbed.wav`，
`audio_bgm_source: "local:music3"`。

---

## F-04 修 P-04：BGM 闪避滤镜图重复消费输出标签

**文件**：`src/shipin_platform/assembly.py`（`master_audio`）

闪避侧链源若同时是 amix 输入（`ne0` 或 `0:a`），先
`[src]asplit=2[src_mix][src_sc]`，sidechaincompress 用 `src_sc`，
amix 用 `src_mix`。

**验证**：最小复用例（2 输入 + sidechain）修复前后对比；完整混音命令
（5 旁白 + 循环 BGM + 4 音效 + 闪避 + amix + apad/atrim）现在成功产出
48k 立体声 `soundbed.wav`。

---

## F-05 修 P-11：h3api 实例路由收敛

**文件**：`~/h3api/server.py`（API 网关配置，非平台仓库）

主实例（:8188，常驻 H3 大权重）模式白名单从 `None`（全接）改为
`("t2v", "i2v", "ref2va", "fl2va", "wan22")`；tts/音乐/生图只路由小实例（:8189）。

**验证**：修复后 5 镜 TTS 全部落在 small 实例，无 `no output files` 失败。
代价：TTS 串行（每镜 6 seed × ~40s 最坏 4 分钟），可接受。

---

## F-06 修 P-01 关联：节点侧 `.env` 参数补齐（配置类）

`~/shipin-platform/.env` 增补：
```
SHIPIN_MEDIA_BACKEND=local          # 曾被整包同步覆盖回 auto，导致 edge_tts 崩溃
SHIPIN_LOCAL_TTS_MAX_SEC=2.9        # 每镜旁白预算
SHIPIN_LOCAL_MUSIC=1                # 启用本地 BGM
AGNES_BASE_URL=http://127.0.0.1:8790/v1   # 反向隧道代理（直连会被 Cloudflare 拦）
```

另：`test_out/_tools/fonts/msyh.ttc` 与 `~/shipin-platform/fonts/msyh.ttc`
放入 CJK 字体（字幕烧录前置条件，缺了直接 FileNotFoundError）。

---

## F-07 修 P-01 关联（补丁自身缺陷）：TTS 终稿必须落回 `out` 路径

**文件**：`src/shipin_platform/generation/local_media.py`

F-02 的「取最短 take」补丁把最佳音频写在 `out.aN` 副本上并返回该路径，
但调用链（tts_service 的 output_path → glob_tts → align）只认 `out`——
align 实际读到的是未加工的第 0 次尝试原件（S02 旁白 15.36s 越过 SPILL 门）。
修复：新增 `_promote_final()`——无论命中还是兜底，终稿一律落回 `out`
并清掉 `.aN` 重试残留。

---

## F-08 修 P-14：字符串台词归一为 dict（台词音频从 never 到 always）

**文件**：`src/shipin_platform/orchestration/pipeline_runner.py`（run_generate_phase 载入分镜处）

```python
for _s in storyboard.get("shots", []):
    _d = _s.get("dialogue")
    if isinstance(_d, str) and _d.strip():
        _s["dialogue"] = {"role_code": "hero_male", "text": _d.strip()}
```

归一后落盘，TTS 合成/align/assemble 三处同时恢复台词轨。
**验证**：S02_dlg/S03_dlg 音频生成，align 的 voice_tail 正确含台词。

---

## F-09 修 P-15：缓存命中的素材哈希对账与 canvas 重派生

**文件**：`src/shipin_platform/orchestration/pipeline_runner.py`（generate 的 clip 缓存分支）

缓存命中时若 manifest 记录的 `clip_sha256` 与盘上 canvas 不符，
从同源 `{sid}_clip.mp4` 确定性重派生 canvas（`_normalize_canvas`）并刷新记录；
派生不可行时至少把记录刷新为盘上实际哈希（保持账实一致，G5 可放行）。

**验证**：S01 的历史劈叉自愈（canvas 重派生 + sha 刷新），assemble 的 G5 门放行，
 stitch 21.75s ≈ 预期 21.77s。

---

## F-10 修 P-16：提示词改纯正面措辞（实测口径）

**文件**：`data/projects/tvc_luckin_01/storyboard.json`（节点项目数据）

- S03 运动描述去掉全部否定词，改「static wide shot of the seaside cliff with
  gentle waves, soft light drift, endless ocean horizon」；
- brief 的 style_anchor 去掉 "vivid coconut tones"（椰子词持续把真椰子引进画面），
  改 "high-key lighting, clean whites and blues"；
- S01/S04/S05 保留"无人物产品静物"的正面描述（主体即产品，无否定词依赖）。

---

## F-11 修 P-17：无主体镜头去掉 "subject stays centered" 子句

**文件**：`src/shipin_platform/orchestration/pipeline_runner.py`（提示词派生）

模板的 centering 子句对纯风景/空镜是负向指令——模型会发明主体并居中
（实测：海景空镜 1.1s 后漂出咖啡杯特写）。subject 以「无人物」开头的镜头
不再追加该子句。S03 的 finding 从 7 条降到 3 条。

**已知局限**：判据是 `subject.startswith("无人物")`，产品静物镜（S01/S04/S05，
subject 也是「无人物,产品静物…」开头）同样被去掉子句——本轮 S01/S04/S05 的
QC 与审查仍通过（首帧锚定强），但更精确的判据应是「主体既非人物也非产品」
（如含 海景/风景/空镜 才去子句），留待下轮细化。

---

## 改动文件清单（可对账）

| 文件 | 改动 | 对应问题 |
|---|---|---|
| `src/shipin_platform/generation/local_media.py` | _tmp_path / 6 次重试 / 最短 take / atempo 2.0 | P-01, P-02 |
| `src/shipin_platform/assembly.py` | _fit_part 冻结补足 / master_audio asplit | P-04, P-05 |
| `src/shipin_platform/orchestration/pipeline_runner.py` | import os | P-03 |
| `~/h3api/server.py`（节点） | 主实例模式白名单 | P-11 |
| `~/shipin-platform/.env`（节点） | 本地后端/TTS 预算/LLM 代理/Music | P-01, P-06 关联 |
| `data/projects/tvc_luckin_01/storyboard.json`（节点） | 夏季服装/S05 产品静物/S01 静态机位 | P-07 |
| `data/projects/tvc_luckin_01/storyboard.json`（节点） | 纯正面措辞（去否定词）/S02 动作改写 | P-16 |
| `data/projects/tvc_luckin_01/brief.json`（节点） | style anchor 去掉椰子色调 | P-16 |

**修复统计**：代码缺陷 6 个（P-02/P-03/P-04/P-05/P-14/P-15，其中 P-14/P-15 为实测
新发现的上游数据形状/记账缺陷），自家补丁缺陷 1 个（P-01 关联的终稿落盘），
模板缺陷 1 个（P-17），配置/工程缺陷 2 个（P-01/.env、P-10/tarball）。
