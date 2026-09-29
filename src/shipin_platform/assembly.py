"""Assembly v2 — narration-first timeline, duration-preserving transitions,
sound design.  融合开源做法（OpenMontage 的 xfade 链、video-agent-kit 系的
「旁白为主轨」思想、xfade-easing 的转场观感），全部用确定性 ffmpeg 实现。

coffee-v5 复盘的两个病根，这里从机制上解决：
1. 配音与画面「各铺各的」——S06 旁白 2.16s 溢出 2.0s 的镜头、S05/S07 旁白
   说完干晾 1.1s+。对齐门按旁白实长计算镜头窗口（window = max(分镜时长,
   tts+气口)），旁白永远在镜头内说完。
2. 成片没有后期——BGM 闪避、切点 whoosh、保时长叠化转场、品牌字卡
   Ken Burns、调色，全部在这里落地为确定性管线步骤。
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

# ffmpeg 8.x 内置 xfade 过渡（xfade-easing 的常见观感子集，无需魔改构建）
XFADE_TRANSITIONS = frozenset({
    "fade", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown",
    "slideleft", "slideright", "slideup", "slidedown", "smoothleft",
    "smoothright", "smoothup", "smoothdown", "circleopen", "circleclose",
    "rectcrop", "distance", "hblur", "pixelize", "radial", "hlwind",
    "diagtl", "diagtr", "diagbl", "diagbr", "squeezeh", "squeezev",
})

DEFAULT_TD = 0.4          # 转场时长（s）
MIN_TAIL = 0.25           # 旁白说完到镜头切走的最小气口（s）
MAX_GAP = 0.8             # 超过即算「干晾」
MASTER_MAX = 10.0         # agnes master 素材上限


def _ffprobe_duration(path: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, shell=False)
    try:
        return float(r.stdout.strip() or 0)
    except ValueError:
        return 0.0


def _ffprobe_size(path: Path) -> Optional[tuple[int, int]]:
    """(width, height)；探测失败/文件不可读返回 None。

    stitch 尺寸预检用——入镜尺寸不一致时 xfade 直接炸且报错不可读
    （C 实证：定向重试循环变量污染使 raw 1280×720 覆盖 canvas 后，
    assemble 炸在 "Could not open encoder before EOF"）。
    """
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, shell=False)
    try:
        w, h = (int(x) for x in r.stdout.strip().split(",")[:2])
        return (w, h)
    except ValueError:
        return None


def _probe_voice_duration(path: Path) -> Optional[float]:
    """轮52(九审 P3-6):人声轨时长探测(align 专用)——坏音频(截断/
    损坏 mp3)旧代码经 _ffprobe_duration 返回 0.0 → 存在性检查与
    voice_tail 都按 0s 算,「旁白比窗还长」整类漏拦(fail-open),
    真实音频长度无约束、压顶下一镜;ffprobe 二进制缺失也走这条
    (FileNotFoundError)。返回 None = 探测失败(调用方必须 fail-closed)。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, shell=False)
    except OSError:
        return None
    out = (r.stdout or "").strip()
    try:
        return float(out)   # 空输出/「N/A」→ ValueError → None(探测失败)
    except ValueError:
        return None


# ── 1. 旁白-镜头对齐门 ────────────────────────────────────────────────


def align_narration(shots: list[dict], min_tail: float = MIN_TAIL,
                    max_gap: float = MAX_GAP,
                    master_duration: float = MASTER_MAX) -> dict:
    """按旁白实长计算每个镜头的最终窗口（旁白驱动剪辑）。

    shots: [{shot_id, duration_sec, narration_path, dialogue_path?}]
    dialogue_path(台词轨)存在时：台词先出，旁白随后跟；窗口取
    台词+气口 与 旁白+气口 中更长者——两条人声都必须落在镜内说完。
    返回 {verdict, timeline, total_sec, findings, next_action}

    规则（确定性）：
    - window = max(storyboard_dur, tts + min_tail)，再 clamp 到 master 可供长度
    - tts + min_tail > master 可供 → SPILL（旁白比素材还长，必须缩句）
    - window - tts > max_gap → DEAD_AIR finding（分镜给的时间太长，缩窗）

    轮49(九审 P2-5)纵深钳制:负 min_tail/max_gap/master_duration 会把
    needed 压到 0 让 SPILL 判据失效(旁白 2s 落进 1s 窗仍判 ok)、负
    master 甚至产出负窗口——API 模型层已拦(AlignRequest validators),
    这里对**内部调用方**同样兜底,不让「负参数=越窗绿」在任何路径成立。
    """
    min_tail = max(float(min_tail), 0.0)
    max_gap = max(float(max_gap), 0.0)
    master_duration = max(float(master_duration), 0.1)
    findings: list[dict] = []
    timeline: list[dict] = []
    t = 0.0
    for s in shots:
        sid = str(s.get("shot_id", "?"))
        sb_dur = float(s.get("duration_sec") or 0)
        np_ = s.get("narration_path")
        dp_ = s.get("dialogue_path")
        tts, dlg = 0.0, 0.0
        # 轮52(九审 P3-6):人声轨探测失败(坏音频/ffprobe 缺失)必须
        # fail-closed——旧代码 0.0 静默让「旁白比窗长」整类漏拦,
        # 真实音频无约束压顶下一镜。文件缺失是另一条路径
        # (NARRATION_MISSING,见下);存在但测不出 = VOICE_PROBE_FAILED。
        if np_ and Path(np_).exists():
            _d = _probe_voice_duration(Path(np_))
            if _d is None:
                findings.append({
                    "severity": "critical", "code": "VOICE_PROBE_FAILED",
                    "message": f"{sid} 旁白音频探测失败(损坏/不可解码/ffprobe 缺失): {np_}"})
            else:
                tts = _d
        if dp_ and Path(dp_).exists():
            _d = _probe_voice_duration(Path(dp_))
            if _d is None:
                findings.append({
                    "severity": "critical", "code": "VOICE_PROBE_FAILED",
                    "message": f"{sid} 台词音频探测失败(损坏/不可解码/ffprobe 缺失): {dp_}"})
            else:
                dlg = _d
        # 轮47(八审 P2#2):纯台词镜(narration 空、dialogue 有词)是合法
        # 剧本形态(模板:每镜 narration 或 dialogue 至少其一,全片至少
        # 2 镜 dialogue)——旧代码只要 narration_path 缺失/不存在就判
        # NARRATION_MISSING,台词轨照常落位也拦。只有两条人声都没有
        # (该镜彻底无声)才是 critical
        if (not np_ or not Path(np_).exists()) and not (
                dp_ and Path(dp_).exists()):
            findings.append({"severity": "critical", "code": "NARRATION_MISSING",
                             "message": f"{sid} 旁白音频不存在: {np_}"})
            tts = 0.0
        # 双轨：台词在窗口起点先出（留 0.1s 起嘴），旁白紧跟台词之后；
        # 没有台词时旁白从起点出。
        voice_tail = tts + dlg + 0.22 if dlg else tts
        needed = voice_tail + min_tail
        window = max(sb_dur, needed) if voice_tail else sb_dur
        window = min(window, master_duration)
        if voice_tail and needed > master_duration:
            findings.append({"severity": "critical", "code": "SPILL",
                             "message": (f"{sid} 声音 {voice_tail:.2f}s + 气口 {min_tail}s "
                                         f"超过素材上限 {master_duration}s——缩句或拆镜")})
        if voice_tail and window - voice_tail > max_gap:
            findings.append({"severity": "suggestion", "code": "DEAD_AIR",
                             "message": (f"{sid} 对白+旁白 {voice_tail:.2f}s / "
                                         f"窗口 {window:.2f}s，"
                                         f"干晾 {window - voice_tail:.2f}s（>{max_gap}s）")})
        # 台词轨定位：台词从窗口起点出，旁白紧跟台词之后 0.18s；
        # 无台词时旁白直接从窗口起点出——两轨都必须落在镜头内。
        if dlg:
            audio_dlg_at = t
            narr_at = t + dlg + 0.18
        else:
            audio_dlg_at = None
            narr_at = t
        timeline.append({
            "shot_id": sid,
            "storyboard_sec": sb_dur,
            "window_sec": round(window, 2),
            "tts_sec": round(tts, 2),
            "dlg_sec": round(dlg, 2),
            "audio_start_sec": round(narr_at, 2),
            # audio_dlg_at 可能恰为 0.0（首镜台词从窗口起点出）——`if x`
            # 会把 0.0 判假成 None，下游 assemble 拿不到 start 就把台词与
            # 旁白同点齐播（A 审计实证：manifest dlg_sec=2.47 但
            # dlg_start_sec=null）。必须 `is not None`。
            "dlg_start_sec": (round(audio_dlg_at, 2)
                              if audio_dlg_at is not None else None),
            "extended": bool(dlg or (tts and window > sb_dur + 0.01)),
        })
        t += window
    verdict = "fix" if any(f["severity"] == "critical" for f in findings) else "ok"
    return {
        "verdict": verdict,
        "timeline": timeline,
        "total_sec": round(t, 2),
        "findings": findings,
        "next_action": ("声音比素材长，缩句后重 TTS" if verdict == "fix"
                        else "把 timeline 的 window_sec 传给 /api/video/stitch（保时长转场）"
                        "，audio_start_sec 用于 /api/audio/master 定位旁白床；"
                        "dlg_start_sec 用于定台词音轨"),
    }


# ── 2. 保时长 xfade 转场拼接 ──────────────────────────────────────────


def _prepend_freeze_head(src: Path, dst: Path, dur: float) -> Path:
    """在片段头部前冻结尾帧 dur 秒——dissolve 边界无 master 时的借位。

    xfade 的重叠区要求入镜有 td 的头帧余量；本地后端没有 master 长素材，
    用入镜自己的首帧冻结补这份余量，叠化作「叠入一瞬静止再启动」的软过渡。
    """
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src),
         "-vf", f"tpad=start_mode=clone:start_duration={max(dur, 0.05):.3f}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(dst)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        raise RuntimeError(f"freeze-head failed: {r.stderr[-300:]}")
    return dst


def _fit_part(src: Path, dst: Path, dur: float) -> Path:
    """裁 src 到 dur 写入 dst；源比目标短时冻结末帧补足。

    align 的窗口语义是「旁白比镜长长则该镜窗口延长」——补料首选 master
    （真实新画面）；没有 master 时若让 part 短于窗口，音画时间线就此劈叉
    （实测：成片 16.67s vs 旁白 20.49s，尾字幕整条溢出被 §10.6 拦下）。
    冻结末帧是该语义的最后兜底，同时按 source=freeze 落账保持透明。
    """
    have = _ffprobe_duration(src)
    if have >= dur - 0.05:
        return _trim(src, dst, dur)
    pad = max(dur - have, 0.05)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src),
         "-vf", f"tpad=stop_mode=clone:stop_duration={pad:.3f}",
         "-t", f"{max(dur, 0.1):.3f}", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-an", str(dst)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        raise RuntimeError(f"pad failed: {r.stderr[-300:]}")
    return dst


def _trim(src: Path, dst: Path, dur: float, head: bool = False) -> Path:
    """裁 src 到 dur 秒写入 dst；head=True 时从尾部回退 dur（保留结尾段）。
    参数全部走字面量列表 + shell=False（无 shell 拼接）。"""
    dur_s = f"{max(dur, 0.1):.3f}"
    if head:
        ss = max(0.0, _ffprobe_duration(src) - dur)
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{ss:.3f}", "-i", str(src),
             "-t", dur_s, "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-an", str(dst)],
            capture_output=True, text=True, shell=False)
    else:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-t", dur_s,
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(dst)],
            capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        raise RuntimeError(f"trim failed: {r.stderr[-300:]}")
    return dst


def build_transition_stitch(clips: list[str], windows: list[float],
                            output: str, transition: str = "dissolve",
                            transition_duration: float = DEFAULT_TD,
                            fps: int = 24,
                            masters: Optional[list[str]] = None,
                            boundary_transitions: Optional[list[str]] = None,
                            card_last: bool = False) -> dict:
    """保时长拼接，支持逐边界转场选型（coffee-v6 教训的机制化）：

    - "cut"：硬切（同场景动作接力）；
    - "softcut"：3 帧软切（xfade fade 0.12s）——同场景换景别用，
      藏掉硬边但不放慢动作；
    - "dissolve" 等 xfade：换场景短叠化。

    重叠区取材优先级（2026-09-27 用户实测「切换生硬」的修复）：
    旧实现一律把 td 的头帧余量加在**入镜**上，本地后端无 master 时
    冻结入镜自己的首帧——新镜头以约 10 帧静止再突然启动，每个 boundary
    都有一次顿挫，这是「生硬」的直接来源。现在重叠区改由**出镜**尾部
    真实素材供给（出镜裁料时多带 td，把真实收尾动作叠进下一镜）；
    出镜本身短于窗口（TTS 拉长窗）时出镜冻结尾帧兜底——上一镜
    「收势让位」比新镜「静止启动」观感自然。softcut 仅 3 帧，即便
    冻结尾尾也完全不可见。

    例外：末镜是 kenburns 落版卡（自带 td 头部余量，card_last=True），
    入侧借帧维持旧行为，落版卡的完整展示时长不被吃掉。

    boundary_transitions: 长度 n-1，每项 "cut"/"softcut"/xfade 名称；
    缺省全用 transition。混合图：连续 xfade 边界分组链式（组内 offset
    用实测长度迭代推进），组间 concat。总长恒 == sum(windows)。
    """
    n = len(clips)
    if n == 0 or len(windows) != n:
        return {"ok": False, "error": "clips/windows 数量不匹配"}
    if boundary_transitions is not None and len(boundary_transitions) != n - 1:
        return {"ok": False, "error": "boundary_transitions 长度必须为 n-1"}
    bts = list(boundary_transitions) if boundary_transitions else \
        [transition] * (n - 1)
    # softcut:3 帧软切——极短 fade 既抹掉硬切边缘,又不构成一次
    # 可感知的「过渡表演」。
    SOFT_TD = max(round(0.12 * fps) / fps, 1.0 / fps)
    td = max(0.1, float(transition_duration))  # dissolve 类边界的统一 td
    for k, bt in enumerate(bts):
        if bt != "cut" and bt != "softcut" and bt not in XFADE_TRANSITIONS:
            return {"ok": False, "error": f"boundary {k}: unknown transition '{bt}'"}
    warnings: list[str] = []
    # 逐边界真实 td 与 xfade 名
    td_of: list[Optional[float]] = []
    xf_of: list[Optional[str]] = []
    for bt in bts:
        if bt == "cut":
            td_of.append(None)
            xf_of.append(None)
        elif bt == "softcut":
            td_of.append(SOFT_TD)
            xf_of.append("fade")
        else:
            td_of.append(max(0.1, float(transition_duration)))
            xf_of.append(bt)

    # 降级判定（2026-09-27 修订）：重叠区改由出镜侧供给（见 docstring），
    # 入镜不再需要头部余量——只有入镜文件本身缺失/不可读才降级硬切。
    # 出镜侧无真实余量（clip 已被窗口吃满）时冻结尾帧兜底：上一镜
    # 「收势让位」的观感远好于旧实现的入镜首帧冻结（新镜静止再启动）。
    for k, td_k in enumerate(td_of):
        if td_k is None:
            continue
        if card_last and k + 1 == n - 1:
            side, idx = "入镜(落版卡)", k + 1
        else:
            side, idx = "出镜", k
        have_c = _ffprobe_duration(Path(clips[idx]))
        m = Path(masters[idx]) if masters and idx < len(masters) and masters[idx] else None
        have_m = _ffprobe_duration(m) if m and m.exists() else 0.0
        if have_c <= 0 and have_m <= 0:
            warnings.append(f"boundary {k}: {side} {Path(clips[idx]).name} "
                            f"不可读，该边界降级硬切")
            bts[k] = "cut"
            td_of[k] = None
            xf_of[k] = None
        elif have_c < windows[idx] + td_k - 0.05 and have_m <= 0:
            warnings.append(
                f"boundary {k}: {side} 无真实余量，冻结尾帧借位 "
                f"{td_k:.2f}s 维持 {bts[k]}（本地后端常态）")

    # 每个 part 需要比窗口多出的时长(由出镜侧或入镜侧边界分担)。
    # 必须在降级判定之后算——被降级硬切的边界不再要求借帧。
    extra = [0.0] * n
    for k, td_k in enumerate(td_of):
        if td_k is None:
            continue
        if card_last and k + 1 == n - 1:
            extra[k + 1] += td_k   # 入落版卡:卡自带余量,旧行为
        else:
            extra[k] += td_k       # 出镜侧供给重叠区(修复默认)

    # 素材可读性前置检查:任何 part 既无 clip 也无 master = fail-fast
    # (带文件名)。旧实现只在 xfade 降级路径上顺带查入镜,入镜不可读时
    # _fit_part 照样去 fit 不存在的文件,抛一句无信息的 "pad failed"。
    for i, cp in enumerate(clips):
        have_c = _ffprobe_duration(Path(cp))
        m = Path(masters[i]) if masters and i < len(masters) and masters[i] else None
        have_m = _ffprobe_duration(m) if m and m.exists() else 0.0
        if have_c <= 0 and have_m <= 0:
            return {"ok": False,
                    "error": (f"part {i} 素材不可读: {Path(cp).name}"
                              f"（既无 clip 也无 master）")}

    # 降级判定（2026-09-27 修订）：重叠区改由出镜侧供给（见 docstring），
    # 入镜不再需要头部余量。出镜侧无真实余量（clip 已被窗口吃满）时冻结
    # 结尾帧兜底：上一镜「收势让位」的观感远好于旧实现的入镜首帧冻结
    # （新镜静止再启动）。素材不可读已在前置检查 fail-fast，不再有
    # 「降级硬切」路径。
    for k, td_k in enumerate(td_of):
        if td_k is None:
            continue
        if card_last and k + 1 == n - 1:
            side, idx = "入镜(落版卡)", k + 1
        else:
            side, idx = "出镜", k
        have_c = _ffprobe_duration(Path(clips[idx]))
        m = Path(masters[idx]) if masters and idx < len(masters) and masters[idx] else None
        have_m = _ffprobe_duration(m) if m and m.exists() else 0.0
        if have_c < windows[idx] + td_k - 0.05 and have_m <= 0:
            warnings.append(
                f"boundary {k}: {side} 无真实余量，冻结尾帧借位 "
                f"{td_k:.2f}s 维持 {bts[k]}（本地后端常态）")

    tmp = Path(tempfile.mkdtemp(prefix="stitch2_"))
    # 轮13:part 来源透明化——align 窗口 > clip 时长时从 master 裁料补足,
    # 该 part 的内容从未过审(clip 审的是另一份)。part 落盘到 output 同级
    # parts/ 并记 source,供 assemble 对 master 补料镜位按实际入拼片段复审
    # (堵"审A拼B":G5 的 clip_sha256 只盯 clip 文件,管不到 master 补料)。
    parts_dir = Path(output).parent / "parts"
    parts_meta: list[dict] = []
    try:
        parts: list[Path] = []
        # 每个 part 的 pad 归属哪一侧:True=入侧(落版卡自带余量,冻首帧),
        # False=出侧(真实尾帧/冻结尾帧)。出侧是修复默认。
        head_pad = [False] * n
        for k, td_k in enumerate(td_of):
            if td_k is None:
                continue
            if card_last and k + 1 == n - 1:
                head_pad[k + 1] = True
        for i in range(n):
            src = Path(clips[i])
            pad = extra[i]
            want = windows[i] + pad
            m = Path(masters[i]) if masters and i < len(masters) and masters[i] else None
            source = "clip"
            if want > _ffprobe_duration(src) + 0.05 and m and m.exists():
                src = _trim(m, tmp / f"m{i:02d}.mp4", want)
                source = "master"
            elif (source == "clip" and pad > 0 and head_pad[i]
                  and want > _ffprobe_duration(src) + 0.05):
                # 入侧借帧仅剩落版卡一种(卡自带 td 余量,通常刚好够,不走这条);
                # 真不够时冻结节帧补,记 freeze-head 与 master/freeze 同规格透明。
                src = _prepend_freeze_head(src, tmp / f"h{i:02d}.mp4", pad)
                source = "freeze-head"
            # 出侧借帧:_fit_part 优先裁真实尾部(clip 够长即零冻结),不够才
            # 冻结尾帧兜底——冻的是上一镜的收势,不是新镜的启动(修复核心)。
            if source == "clip" and want > _ffprobe_duration(src) + 0.05:
                source = "freeze"
            if source == "master":
                # master 也可能短于 want(云端固定 5.167s < align 窗口):
                # _fit_part 会冻结尾帧补足,这段内容从未过审——必须如实标注,
                # 否则冻结被记成 master,B 实测 100% 隐藏(parts 帧差 mean≈0.0005)
                if _ffprobe_duration(src) < want - 0.05:
                    source = "master+freeze"
            parts.append(_fit_part(src, tmp / f"p{i:02d}.mp4", want))
            parts_meta.append({"idx": i, "source": source,
                               "src": str(m if source == "master"
                                          else Path(clips[i])),
                               "want_sec": round(want, 3)})
        parts_dir.mkdir(parents=True, exist_ok=True)
        # 尺寸预检(A 审计问题 #6):xfade 要求全部入镜尺寸一致,但 clip 的
        # 画布归一化(720x1280)是按 shot 做的——重试轮/缓存轮可能漏掉某镜
        # (实测 drama S03_canvas 768x1344 混入 720x1280 队列,xfade 直接
        # 炸且报错不可读)。这里统一以第一镜尺寸为基准,不符的就地归一化,
        # 不再把尺寸问题留到 ffmpeg 里 late-fail。
        # 2026-09-26 用户反馈「比例必须一刀切」:与 _normalize_canvas 同一
        # 策略——increase+crop 裁剪填充,绝不用 decrease+pad 信箱(横屏进竖屏
        # 会留 58% 黑边,同片内比例观感劈叉)。
        ref_size = _ffprobe_size(parts[0]) if parts else None
        if ref_size:
            for i, p in enumerate(parts):
                if _ffprobe_size(p) != ref_size:
                    rw, rh = ref_size
                    fixed = p.with_name(p.stem + "_fix.mp4")
                    r = subprocess.run(
                        ["ffmpeg", "-y", "-i", str(p),
                         "-vf", f"scale={rw}:{rh}:force_original_aspect_ratio=increase,"
                                f"crop={rw}:{rh},setsar=1",
                         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(fixed)],
                        capture_output=True, text=True, shell=False)
                    if r.returncode == 0:
                        parts[i] = fixed
                        warnings.append(
                            f"part {i}: 入镜尺寸 {(_ffprobe_size(p) or ('?','?'))[0]}x"
                            f"{(_ffprobe_size(p) or ('?','?'))[1]} 与基准 {rw}x{rh} "
                            f"不符，已裁剪填充（比例一刀切，禁止信箱/拉伸）")
                        parts_meta[i]["source"] = str(parts_meta[i].get("source")) + "+cropfill"
        for i, p in enumerate(parts):
            dst = parts_dir / f"p{i:02d}.mp4"
            shutil.copyfile(p, dst)
            parts_meta[i]["part_path"] = str(dst)

        # 分组：xfade 边界连接的 part 为一组（组内 xfade 链），组间 concat
        groups: list[list[int]] = [[0]]
        for k in range(1, n):
            if bts[k - 1] != "cut":
                groups[-1].append(k)
            else:
                groups.append([k])

        inputs: list[str] = []
        for p in parts:
            inputs += ["-i", str(p)]
        fg: list[str] = []
        group_labels: list[str] = []
        consumed = 0

        for gi, grp in enumerate(groups):
            if len(grp) == 1:
                fg.append(f"[{consumed}:v]null[g{gi}]")
                group_labels.append(f"[g{gi}]")
                consumed += 1
                continue
            prev = f"{consumed}:v"
            chain_len = _ffprobe_duration(parts[grp[0]])
            for j, idx in enumerate(grp[1:], start=1):
                k = grp[j] - 1  # 入镜 idx 的入边界
                td_k = td_of[k] if td_of[k] is not None else 0.1
                offset = max(chain_len - td_k, 0.0)
                label = f"x{gi}_{j}"
                fg.append(f"[{prev}][{consumed + j}:v]xfade=transition={xf_of[k]}"
                          f":duration={td_k:.3f}:offset={offset:.3f}[{label}]")
                prev = label
                chain_len = offset + _ffprobe_duration(parts[grp[j]])
            group_labels.append(f"[{prev}]")
            consumed += len(grp)

        if len(groups) == 1:
            final_map = group_labels[0].strip("[]")
        else:
            fg.append(f"{''.join(group_labels)}concat=n={len(groups)}:v=1:a=0[vout]")
            final_map = "vout"
        r = subprocess.run(
            ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fg),
             "-map", f"[{final_map}]", "-c:v", "libx264", "-pix_fmt",
             "yuv420p", "-an", str(output)],
            capture_output=True, text=True, shell=False)
        if r.returncode != 0:
            return {"ok": False, "error": r.stderr[-500:]}
        dur = _ffprobe_duration(Path(output))
        out = {"ok": True, "output": str(output), "duration": round(dur, 2),
               "expected_sec": round(sum(windows), 2),
               "transitions": bts, "transition_duration": td,
               "transitions_dur": [round(t, 3) if t else None for t in td_of],
               "parts": parts_meta,
               "boundary_preserved": abs(dur - sum(windows)) <= 0.25}
        if warnings:
            out["warnings"] = warnings
        return out
    finally:
        for p in tmp.iterdir():
            p.unlink(missing_ok=True)
        tmp.rmdir()


# ── 3. 声音设计：whoosh 合成 + BGM 闪避主混音 ─────────────────────────


def synth_whoosh(out: str, dur: float = 0.5, kind: str = "whoosh") -> str:
    """确定性合成转场音效（无需音频素材库）：粉噪 + 滤波 + 包络。"""
    out_p = Path(out)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    if kind == "pop":
        af = ("sine=frequency=520:duration=0.12,"
              "afade=t=in:d=0.005,afade=t=out:st=0.06:d=0.06,volume=0.7")
    else:
        fade_out_st = f"{dur * 0.45:.3f}"
        af = (f"anoisesrc=colour=pink:duration={dur:.3f}:amplitude=0.55,"
              "lowpass=f=1800,highpass=f=280,"
              f"afade=t=in:d={dur * 0.45:.3f},afade=t=out:st={fade_out_st}"
              f":d={dur * 0.55:.3f},volume=0.6")
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", af,
         "-ar", "48000", "-ac", "2", str(out_p)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        raise RuntimeError(f"sfx synth failed: {r.stderr[-300:]}")
    return str(out_p)


def master_audio(narration_path: Optional[str], duration_sec: float, output: str,
                 bgm_path: Optional[str] = None, bgm_gain_db: float = -19.0,
                 duck: bool = True,
                 sfx_events: Optional[list[dict]] = None,
                 narration_events: Optional[list[dict]] = None,
                 work_dir: Optional[str] = None) -> dict:
    """最终声音设计主混音 → 单条 48k 立体声。

    二选一或并用：
    - narration_path: 已拼好的旁白床
    - narration_events: [{path, time}] 逐镜旁白定位（平台内部 adelay，
      agent 不再自己混旁白床——coffee-v5 的 adelay=0:0 崩溃就是那步出的）
    sfx_events: [{"time": 4.0, "kind": "whoosh"|"pop", "gain_db": -6}]
    BGM 用 sidechaincompress 被旁白闪避（说话时音乐退后，气口时浮上来）。
    """
    tmp = Path(work_dir or tempfile.mkdtemp(prefix="masteraud_"))
    tmp.mkdir(parents=True, exist_ok=True)
    if not narration_path and not narration_events:
        return {"ok": False, "error": "narration_path 或 narration_events 必须提供一个"}

    narration: Optional[Path] = None
    if narration_path:
        narration = Path(narration_path)
        if not narration.exists():
            return {"ok": False, "error": f"narration not found: {narration}"}
    if bgm_path and not Path(bgm_path).exists():
        return {"ok": False, "error": f"bgm not found: {bgm_path}"}

    sfx_files: list[tuple[float, Path, float]] = []
    for i, ev in enumerate(sfx_events or []):
        t = float(ev.get("time", 0))
        gain = float(ev.get("gain_db", -6.0))
        p = Path(ev["path"]) if ev.get("path") else \
            Path(tmp) / f"sfx_{i:02d}.wav"
        if not ev.get("path"):
            synth_whoosh(str(p), dur=0.5, kind=ev.get("kind", "whoosh"))
        sfx_files.append((t, p, gain))

    inputs: list[str] = []
    mix_inputs: list[str] = []
    fg: list[str] = []
    idx = 0
    if narration:
        inputs += ["-i", str(narration)]
        mix_inputs.append("0:a")
        idx = 1
    for j, ev in enumerate(narration_events or []):
        p = Path(ev["path"])
        if not p.exists():
            return {"ok": False, "error": f"narration event {j} not found: {p}"}
        inputs += ["-i", str(p)]
        delay_ms = max(int(float(ev.get("time", 0)) * 1000), 1)
        g = 10 ** (float(ev.get("gain_db", 0.0)) / 20)
        fg.append(f"[{idx}:a]volume={g:.4f},adelay={delay_ms}|{delay_ms}[ne{j}]")
        mix_inputs.append(f"ne{j}")
        idx += 1
    if bgm_path:
        inputs += ["-stream_loop", "-1", "-i", str(bgm_path)]
        g = 10 ** (bgm_gain_db / 20)
        fg.append(f"[{idx}:a]volume={g:.4f}[bgm0]")
        if duck:
            # 闪避 sidechain 取主旁白（0 号或第一个旁白事件）
            side = "0:a" if narration else mix_inputs[0]
            sc_src = side
            if side in mix_inputs:
                # 该标签还要进 amix——ffmpeg 的同一输出标签不能被两个输入
                # 消费（报 "Invalid stream specifier / matches no streams"），
                # 先 asplit 一份专供闪避，amix 用改名后的那份。
                mix_label = side.replace(":", "_").replace("[", "").replace("]", "")
                fg.append(f"[{side}]asplit=2[{mix_label}][{side}_sc]")
                mix_inputs = [mix_label if x == side else x for x in mix_inputs]
                sc_src = f"{side}_sc"
            fg.append(f"[bgm0][{sc_src}]sidechaincompress=threshold=0.02:ratio=8"
                      f":attack=40:release=500[bgm]")
        else:
            fg.append(f"[{idx}:a]anull[bgm]")
        mix_inputs.append("bgm")
        idx += 1
    for j, (t, p, gain) in enumerate(sfx_files):
        inputs += ["-i", str(p)]
        delay_ms = max(int(t * 1000), 1)
        g = 10 ** (gain / 20)
        fg.append(f"[{idx}:a]volume={g:.4f},adelay={delay_ms}|{delay_ms}[s{j}]")
        mix_inputs.append(f"s{j}")
        idx += 1
    dur_s = f"{max(duration_sec, 0.5):.3f}"
    fg.append("".join(f"[{x}]" for x in mix_inputs)
              + f"amix=inputs={len(mix_inputs)}:duration=longest:normalize=0,"
              + f"apad=whole_dur={dur_s},atrim=0:{dur_s}[out]")

    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(fg),
         "-map", "[out]", "-ar", "48000", "-ac", "2", str(out)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-500:]}
    return {"ok": True, "output": str(out),
            "duration": round(_ffprobe_duration(out), 2),
            "bgm_ducked": bool(bgm_path and duck),
            "narration_events": len(narration_events or []),
            "sfx_count": len(sfx_files)}


# ── 5. 音画合流与响度归一（平台内核心，API 与 runner 共用）────────────


def mux_audio_video(video_path: str, audio_path: str, output: str,
                    shortest: bool = True, audio_offset: float = 0.0) -> dict:
    """旁白/主混音合入视频：视频流 copy（无损），音频 AAC 192k。"""
    v, a, out = Path(video_path), Path(audio_path), Path(output)
    if not v.exists():
        return {"ok": False, "error": f"video not found: {v}"}
    if not a.exists():
        return {"ok": False, "error": f"audio not found: {a}"}
    out.parent.mkdir(parents=True, exist_ok=True)
    offset_args = ["-itsoffset", f"{audio_offset:.3f}"] if audio_offset > 0 else []
    shortest_args = ["-shortest"] if shortest else []
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(v), "-i", str(a), *offset_args,
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         *shortest_args, str(out)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-400:]}
    return {"ok": True, "output": str(out)}


def normalize_loudness(audio_path: str, output: str, target_lufs: float = -14.0,
                       tp_dbtp: float = -1.5, lra: float = 11.0,
                       two_pass: bool = True) -> dict:
    """EBU R128 two-pass loudnorm（与 /api/audio/normalize 同实现）。"""
    inp = Path(audio_path)
    if not inp.exists():
        return {"ok": False, "error": f"not found: {inp}"}
    out = Path(output)
    out.parent.mkdir(parents=True, exist_ok=True)
    af = f"loudnorm=I={target_lufs}:TP={tp_dbtp}:LRA={lra}"
    if two_pass:
        r1 = subprocess.run(
            ["ffmpeg", "-y", "-i", str(inp),
             "-af", af + ":print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, shell=False)
        m = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', r1.stderr)
        if m:
            try:
                measured = json.loads(m[-1])
                af = (f"loudnorm=I={target_lufs}:TP={tp_dbtp}:LRA={lra}"
                      f":measured_I={measured.get('input_i')}"
                      f":measured_TP={measured.get('input_tp')}"
                      f":measured_LRA={measured.get('input_lra')}"
                      f":measured_thresh={measured.get('input_thresh')}"
                      f":offset={measured.get('target_offset')}:linear=true")
            except json.JSONDecodeError:
                pass
    enc = ["-c:a", "pcm_s16le", "-ar", "48000"] if str(out).lower().endswith(".wav") \
        else ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(inp), "-af", af, *enc, str(out)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-400:]}
    return {"ok": True, "output": str(out)}


def color_grade_warm(src: str, output: str) -> dict:
    """warm_tvc 调色（与 /api/video/color-grade 预设一致）。"""
    s, o = Path(src), Path(output)
    if not s.exists():
        return {"ok": False, "error": f"not found: {s}"}
    preset = ("curves=r='0/0 0.3/0.28 0.7/0.72 1/1',"
              "curves=g='0/0 0.5/0.5 1/1',"
              "curves=b='0/0.05 0.7/0.68 1/0.95'")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(s), "-vf", preset,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(o)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-400:]}
    return {"ok": True, "output": str(o)}


def burn_srt(video_path: str, srt_path: str, output: str,
             font_size: int = 46, margin_v: int = 96) -> dict:
    """字幕烧录（libass 探测 → drawtext 回退，带 cue 验收）。"""
    from shipin_platform.tools.subtitle_renderer import render_subtitles_best
    return render_subtitles_best(video_path, srt_path, output,
                                 font_size=font_size, margin_v=margin_v)


def loudness_measure(path: str) -> dict:
    """实测响度（loudnorm pass-1 测量，用于交付报告的 LUFS 复核）。"""
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"not found: {p}"}
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(p),
         "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
         "-f", "null", "-"],
        capture_output=True, text=True, shell=False)
    m = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', r.stderr)
    if not m:
        return {"ok": False, "error": "loudnorm measure failed"}
    try:
        d = json.loads(m[-1])
        return {"ok": True, "input_i": float(d.get("input_i", 0)),
                "input_tp": float(d.get("input_tp", 0))}
    except (json.JSONDecodeError, ValueError, TypeError):
        return {"ok": False, "error": "measure parse failed"}


# ── 4. 品牌字卡 Ken Burns（静态卡运动感）──────────────────────────────


def kenburns(image_path: str, duration: float, output: str,
             zoom_to: float = 1.10, fps: int = 24,
             size: str = "1280x704") -> dict:
    """静态图 → 缓慢推近的视频片段（品牌落版不再死板）。"""
    src = Path(image_path)
    if not src.exists():
        return {"ok": False, "error": f"image not found: {src}"}
    w, h = size.split("x")
    frames = max(int(duration * fps), fps)
    zexpr = f"min(1+({zoom_to - 1:.4f}*on/{frames}),{zoom_to:.3f})"
    vf = (f"scale={w}:{h},setsar=1,"
          f"zoompan=z='{zexpr}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
          f":d={frames}:s={size}:fps={fps}")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loop", "1", "-i", str(src), "-vf", vf,
         "-t", f"{max(duration, 0.5):.3f}", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", "-an", str(output)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        return {"ok": False, "error": r.stderr[-400:]}
    return {"ok": True, "output": str(output),
            "duration": round(_ffprobe_duration(Path(output)), 2)}
