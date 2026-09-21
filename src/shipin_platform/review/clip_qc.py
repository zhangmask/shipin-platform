"""Per-clip hard QC gate — deterministic, no model API required.

 disease this module exists for: a single storyboard shot comes back from the
video model containing 3-4 self-invented sub-shots (hard cuts inside one
clip), a static slideshow instead of motion, or a scene that has nothing to
do with the storyboard's first frame.  None of that is visible to the
rule-based text reviews, and the old final-video VLM gate sampled 12 uniform
frames that could never land on a 1.2s sub-shot boundary.  This gate runs
right after each clip is generated, so a bad clip is re-rolled before it can
ever reach the timeline.

Checks (all ffmpeg + PIL, deterministic):
  1. duration vs the storyboard's expected duration (± tolerance)
  2. resolution match (optional)
  3. internal hard cuts via ffmpeg scene-select  (default: 0 allowed)
  4. motion energy floor — slideshow / frozen-frame detection
  5. first frame vs reference image perceptual distance (dHash, 0-64)
Optional VLM same-scene check (use_vlm=True) degrades gracefully without a key.
"""
from __future__ import annotations

import base64
import json
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

# ── ffprobe helpers ───────────────────────────────────────────────────


def _ffprobe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError(f"ffprobe failed for {path}")
    return json.loads(r.stdout)


def _duration_and_dims(path: Path) -> tuple[float, tuple[int, int]]:
    data = _ffprobe(path)
    dur = float(data.get("format", {}).get("duration", 0) or 0)
    v = next((s for s in data.get("streams", [])
              if s.get("codec_type") == "video"), {})
    return dur, (int(v.get("width", 0) or 0), int(v.get("height", 0) or 0))


# ── scene-cut detection ───────────────────────────────────────────────


def detect_cut_times(video: Path, threshold: float = 0.3) -> list[float]:
    """Timestamps (s) where ffmpeg's scene score exceeds ``threshold``."""
    r = subprocess.run(
        ["ffmpeg", "-i", str(video),
         "-vf", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
        capture_output=True, text=True)
    times = []
    for m in re.finditer(r"pts_time:([0-9.]+)", r.stdout + r.stderr):
        times.append(round(float(m.group(1)), 3))
    return sorted(set(times))


# ── frame sampling / motion ───────────────────────────────────────────


def _sample_gray_frames(video: Path, fps: float = 2.0, size=(64, 36),
                        max_frames: int = 40) -> list:
    from PIL import Image
    tmp = tempfile.mkdtemp(prefix="clipqc_")
    subprocess.run(
        ["ffmpeg", "-i", str(video), "-vf",
         f"fps={fps},scale={size[0]}:{size[1]}", "-pix_fmt", "gray",
         "-frames:v", str(max_frames), "-y", str(Path(tmp) / "f%03d.png")],
        capture_output=True, text=True)
    frames = []
    for p in sorted(Path(tmp).glob("f*.png")):
        try:
            frames.append(Image.open(p).convert("L"))
        except OSError:
            continue
    return frames


def motion_energy(video: Path) -> float:
    """Mean absolute gray-level delta between consecutive sampled frames
    (0-255 scale).  A frozen/slideshow clip sits near 0; real footage of any
    kind (even slow dolly) measures several units."""
    frames = _sample_gray_frames(video)
    if len(frames) < 2:
        return 0.0
    total = 0.0
    n = 0
    for a, b in zip(frames, frames[1:]):
        pa, pb = a.load(), b.load()
        w, h = a.size
        s = 0
        for y in range(0, h, 2):
            for x in range(0, w, 2):
                s += abs(pa[x, y] - pb[x, y])
        total += s / ((w // 2) * (h // 2))
        n += 1
    return round(total / max(n, 1), 3)


def transient_spikes(video: Path, fps: float = 8.0,
                     jump: float = 70.0,
                     max_frames: int = 400) -> list[dict]:
    """Scan the whole clip for transient brightness spikes (flash frames).

    审计 G1（2026-09-21）：vlm_morph_check 只在 3 个时间点采样、motion_energy
    把 8~20 个 Δ 的平均压平——0.2~0.5s 的闪白/闪黑/单帧崩坏落在两者之间时
    会整段漏掉。这里用较低的 fps 全片扫描帧亮度均值，任何「单帧/双帧亮度
    相对两侧帧跳变 ≥ jump/255」的瞬态都会被记下。纯确定性、零 LLM 成本。

    Returns [{"t": seconds, "delta": max_luminance_jump, "frames": spike_width}]
    """
    frames = _sample_gray_frames(video, fps=fps, size=(32, 18),
                                 max_frames=max_frames)
    if len(frames) < 3:
        return []
    means = []
    for im in frames:
        h = im.histogram()
        total = sum(i * c for i, c in enumerate(h))
        means.append(total / float(im.size[0] * im.size[1]))
    out = []
    i = 1
    while i < len(means) - 1:
        m, prev, nxt = means[i], means[i - 1], means[i + 1]
        d_prev, d_nxt = abs(m - prev), abs(m - nxt)
        # 单帧尖峰:与两侧都比 |jump| 大
        if d_prev >= jump and d_nxt >= jump:
            out.append({"t": round(i / fps, 3), "delta": round(max(d_prev, d_nxt), 1),
                        "t_width": 1})
            i += 2
            continue
        # 双帧亮/暗带(如闪两帧):m 与 prev 突跳, 且 nxt 与 m 同向(带内)
        if d_prev >= jump and abs(nxt - m) <= jump * 0.35:
            j = i + 1
            while j < len(means) - 1 and abs(means[j] - m) <= jump * 0.3:
                j += 1
            out.append({"t": round(i / fps, 3),
                        "delta": round(d_prev, 1),
                        "t_width": j - i})
            i = j + 1
            continue
        i += 1
    return out


# ── dHash perceptual distance ─────────────────────────────────────────


def _dhash(img, size=8) -> int:
    from PIL import Image
    img = img.convert("L").resize((size + 1, size))
    px = img.load()
    bits = 0
    bit = 0
    for y in range(size):
        for x in range(size):
            if px[x, y] > px[x + 1, y]:
                bits |= (1 << bit)
            bit += 1
    return bits


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _first_frame_image(video: Path, t: float = 0.2):
    from PIL import Image
    import tempfile
    tmp = tempfile.mkdtemp(prefix="clipqc_ff_")
    out = Path(tmp) / "first.png"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(t), "-i", str(video),
         "-frames:v", "1", "-y", str(out)],
        capture_output=True, text=True)
    if not out.exists():
        return None
    return Image.open(out)


# ── optional VLM same-scene check ─────────────────────────────────────


def vlm_same_scene(reference_image: Path, clip_first_frame_image: Path) -> dict:
    """Ask the VLM whether the clip's first frame shows the same scene/subject
    as the reference first-frame image.  Returns {"available", "verdict",
    "reason"}; never raises."""
    from .hard_gates import _vlm_credentials, _ask_vlm
    key = _vlm_credentials()
    if not key:
        return {"available": False, "verdict": "unknown",
                "reason": "AGNES_KEY 未配置，跳过 VLM 一致性检查"}
    try:
        imgs = []
        for p in (reference_image, clip_first_frame_image):
            with open(p, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            imgs.append({"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}})
        prompt = (
            "两张图：第一张是分镜指定的首帧参考图，第二张是生成视频的实际首帧。"
            "只依据画面事实回答：两者是否为同一场景、同一主体（允许构图/光线有差异，"
            "但主体身份、场景类型必须一致）。"
            '返回严格 JSON：{"same_scene": true/false, "reason": "..."}'
        )
        resp = _ask_vlm(imgs, prompt, key, max_tokens=300)
        m = re.search(r"\{.*\}", resp, re.S)
        parsed = json.loads(m.group(0)) if m else {}
        return {"available": True, "verdict":
                "same" if parsed.get("same_scene") else "different",
                "reason": str(parsed.get("reason", ""))[:200]}
    except Exception as e:
        return {"available": False, "verdict": "unknown",
                "reason": f"VLM 检查失败: {type(e).__name__}: {e}"[:200]}


def vlm_morph_check(clip_path: str, samples: int = 3) -> dict:
    """VLM 检查素材内部是否存在「主体形态突变/变形」——A/B/C 实测的最常见
    病(咖啡粉瞬间变整颗豆)。抽首/中/尾 3 帧问同一主体。never raises."""
    from .hard_gates import _vlm_credentials, _ask_vlm
    key = _vlm_credentials()
    if not key:
        return {"available": False, "verdict": "unknown",
                "reason": "AGNES_KEY 未配置，跳过 VLM 变形检查"}
    try:
        dur, _dims = _duration_and_dims(Path(clip_path))
        tmp = tempfile.mkdtemp(prefix="clipqc_morph_")
        ts = [dur * (i + 0.5) / samples for i in range(samples)]
        paths = []
        for j, t in enumerate(ts):
            p = Path(tmp) / f"m{j}.png"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", clip_path,
                 "-frames:v", "1", "-y", str(p)],
                capture_output=True, text=True)
            if p.exists() and p.stat().st_size > 1000:
                paths.append(p)
        if not paths:
            return {"available": False, "verdict": "unknown",
                    "reason": "抽帧全部失败"}
        imgs = []
        for p in paths:
            with open(p, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            imgs.append({"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}})
        prompt = (
            f"这是同一段视频按时间顺序采样的 {len(paths)} 帧(t 递增)。"
            "只依据画面事实回答：镜头内是否存在主体形态突变或物体变形"
            "(例如粉末瞬间变整颗粒、人形/物体形态异变、材质突变等违反"
            "物理的形态变化)？光线/构图/位置微变不算。"
            '返回严格 JSON：{"morph": true/false, "reason": "..."}'
        )
        resp = _ask_vlm(imgs, prompt, key, max_tokens=300)
        m = re.search(r"\{.*\}", resp, re.S)
        parsed = json.loads(m.group(0)) if m else {}
        return {"available": True,
                "verdict": "morph" if parsed.get("morph") else "intact",
                "reason": str(parsed.get("reason", ""))[:200]}
    except Exception as exc:
        return {"available": False, "verdict": "unknown",
                "reason": f"VLM 变形检查失败: {type(exc).__name__}: {exc}"[:200]}


# ── the gate itself ───────────────────────────────────────────────────


def qc_clip(
    clip_path: str,
    shot_id: str = "",
    expected_duration_sec: Optional[float] = None,
    duration_tolerance: float = 0.7,
    reference_image: Optional[str] = None,
    ref_hash_fail: int = 46,
    ref_hash_warn: int = 32,
    scene_threshold: float = 0.3,
    max_internal_cuts: int = 0,
    check_motion: bool = True,
    motion_floor: float = 1.0,
    expected_resolution: Optional[list] = None,
    use_vlm: bool = False,
) -> dict:
    """Run every deterministic check on one generated clip.

    Returns {"verdict": "ok"|"fix", "checks": {...}, "findings": [...],
    "next_action": ...}.  ``verdict == "fix"`` means the clip must be
    regenerated (or explicitly waived by the user) — it must never be stitched.
    """
    findings: list[dict] = []
    checks: dict = {}
    v = Path(clip_path)
    if not v.exists():
        return {"shot_id": shot_id, "clip_path": str(v), "verdict": "fix",
                "checks": {}, "next_action": "重新生成该镜头素材",
                "findings": [{"severity": "critical", "code": "CLIP_MISSING",
                              "message": f"素材文件不存在: {v}"}]}

    # 1) duration
    try:
        dur, dims = _duration_and_dims(v)
    except RuntimeError as e:
        return {"shot_id": shot_id, "clip_path": str(v), "verdict": "fix",
                "checks": {}, "next_action": "素材损坏，重新生成",
                "findings": [{"severity": "critical", "code": "PROBE_FAILED",
                              "message": str(e)}]}
    checks["duration"] = {"value": round(dur, 2), "expected": expected_duration_sec}
    if expected_duration_sec and abs(dur - float(expected_duration_sec)) > duration_tolerance:
        findings.append({
            "severity": "critical", "code": "DURATION_MISMATCH",
            "message": (f"镜头{shot_id} 实际 {dur:.2f}s ≠ 分镜预期 "
                        f"{expected_duration_sec}s（容差 {duration_tolerance}s）")})

    # 2) resolution
    checks["resolution"] = {"value": list(dims), "expected": expected_resolution}
    if expected_resolution and list(dims) != [int(expected_resolution[0]),
                                              int(expected_resolution[1])]:
        findings.append({
            "severity": "critical", "code": "RESOLUTION_MISMATCH",
            "message": f"镜头{shot_id} 分辨率 {dims} ≠ 预期 {expected_resolution}"})

    # 3) internal hard cuts — the "one shot, many sub-shots" disease
    cuts = detect_cut_times(v, threshold=scene_threshold)
    checks["internal_cuts"] = {"value": len(cuts), "max_allowed": max_internal_cuts,
                               "times": cuts, "threshold": scene_threshold}
    if len(cuts) > max_internal_cuts:
        findings.append({
            "severity": "critical", "code": "INTERNAL_CUTS",
            "message": (f"镜头{shot_id} 内部检测到 {len(cuts)} 次硬切 "
                        f"(t={cuts})——视频模型在单镜头内私开子镜头，"
                        f"这是『画面凌乱/换镜太快』的直接病根，必须重新生成")})

    # 4) motion energy
    checks["motion"] = {"enabled": check_motion}
    if check_motion:
        me = motion_energy(v)
        checks["motion"]["value"] = me
        checks["motion"]["floor"] = motion_floor
        if me < motion_floor:
            findings.append({
                "severity": "critical", "code": "STATIC_SLIDESHOW",
                "message": (f"镜头{shot_id} 运动能量 {me} 低于下限 {motion_floor}"
                            f"——疑似静态图/幻灯片，而不是视频")})

    # 4b) transient brightness spikes (flash / 单帧崩坏) — 审计 G1
    #     (vlm_morph_check 只采 3 点、motion_energy 取平均,0.2~0.5s 的
    #     闪白/闪黑/单帧异常夹在两者之间必漏;这里是逐帧亮度瞬变扫描)
    spikes = transient_spikes(v)
    checks["transient"] = {"count": len(spikes), "spikes": spikes,
                           "jump": 70.0, "scale": "0-255 亮度跳变"}
    for _s in spikes:
        findings.append({
            "severity": "critical", "code": "TRANSIENT_FLASH",
            "message": (f"镜头{shot_id} t={_s['t']}s 存在亮度瞬变"
                        f"(Δ={_s['delta']}/255, 持续 {_s['t_width']} 个采样帧)"
                        f"——闪白/闪黑/单帧崩坏帧,必须重新生成")})

    # 5) first frame vs reference image
    checks["reference_match"] = {"enabled": bool(reference_image)}
    vlm_note = None
    if reference_image:
        ref = Path(reference_image)
        if not ref.exists():
            findings.append({"severity": "critical", "code": "REF_MISSING",
                             "message": f"参考首帧图不存在: {ref}"})
        else:
            from PIL import Image
            ff_img = _first_frame_image(v)
            if ff_img is None:
                findings.append({"severity": "critical", "code": "FIRST_FRAME_FAIL",
                                 "message": "无法从素材抽取首帧"})
            else:
                dist = _hamming(_dhash(Image.open(ref)), _dhash(ff_img))
                checks["reference_match"]["dhash_hamming"] = dist
                checks["reference_match"]["scale"] = "0-64 (0=identical)"
                if dist > ref_hash_fail:
                    findings.append({
                        "severity": "critical", "code": "REF_MISMATCH",
                        "message": (f"镜头{shot_id} 首帧与参考图感知距离 {dist} "
                                    f"(fail>{ref_hash_fail})——生成的画面与分镜"
                                    f"首帧不是同一场景")})
                elif dist > ref_hash_warn:
                    findings.append({
                        "severity": "suggestion", "code": "REF_DRIFT",
                        "message": f"镜头{shot_id} 首帧与参考图距离 {dist} 偏大 "
                                   f"(warn>{ref_hash_warn})，建议人工复核"})
                if use_vlm:
                    tmp_ff = Path(tempfile.mkdtemp(prefix="clipqc_vlm_")) / "ff.png"
                    ff_img.save(tmp_ff)
                    res = vlm_same_scene(ref, tmp_ff)
                    checks["reference_match"]["vlm"] = res
                    if res.get("available") and res.get("verdict") == "different":
                        findings.append({
                            "severity": "critical", "code": "REF_VLM_MISMATCH",
                            "message": f"镜头{shot_id} VLM 判定首帧与参考图"
                                       f"不是同一场景: {res.get('reason', '')}"})
                    elif not res.get("available"):
                        vlm_note = res.get("reason")

    # 6) morph check (VLM):同一镜头内主体形态突变 → 重试
    #    (M7-2, 2026-09-21 A/B 实测: 粉→整豆变形,确定性检测查不出,只有 VLM 能)
    checks["morph"] = {"enabled": use_vlm}
    if use_vlm:
        mres = vlm_morph_check(str(v))
        checks["morph"]["result"] = mres
        if mres.get("available") and mres.get("verdict") == "morph":
            findings.append({
                "severity": "critical", "code": "MORPH_DETECTED",
                "message": (f"镜头{shot_id} VLM 判定镜头内部存在主体形态突变"
                            f"(变形/异变): {mres.get('reason', '')}")})
        elif not mres.get("available"):
            vlm_note = (vlm_note or "") + " " + str(mres.get("reason") or "")
            vlm_note = vlm_note.strip()

    verdict = "fix" if any(f["severity"] == "critical" for f in findings) else "ok"
    if verdict == "ok":
        next_action = "通过，可进入下一镜头"
    elif any(f["code"] == "INTERNAL_CUTS" for f in findings):
        next_action = ("重新生成：在提示词中明确『single continuous shot, no cuts』"
                       "并使用首尾帧锚定；重试后仍出现内部切换则换模型或拆分镜头")
    elif any(f["code"] == "MORPH_DETECTED" for f in findings):
        next_action = ("重新生成：主体形态突变→在提示词追加『the subject keeps its "
                       "exact shape and material throughout, no morphing』，"
                       "并避免让同镜内出现『形态A变形态B』的两态动作")
    else:
        next_action = "按 findings 重新生成该镜头"
    result = {"shot_id": shot_id, "clip_path": str(v), "verdict": verdict,
              "checks": checks, "findings": findings, "next_action": next_action}
    if vlm_note:
        result["note"] = vlm_note
    return result
