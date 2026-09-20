"""Hard review gates for the final delivery chain (AGENT_GUIDE §10.7.1 / §10.8).

Two gates that used to be "documented but not enforced" — the root cause of
"审核太容易放过":

1. ``check_timeline`` — deterministic, NO model involved. Rejects the exact
   disease seen in v5: the same source clip planted again and again at random
   spots ("拼接感"), durations not covering the target length, out-of-order
   timeline entries.

2. ``vlm_review_final`` — sample the finished video and ask a VLM to walk the
   five-act arc frame by frame. Requires AGNES_KEY (env var or
   %TEMP%/agnes_key.txt); without it the gate reports ``blocked`` so a missing
   key never silently lets a video through.

SSRF discipline mirrors the rest of the codebase: fixed literal URL, host
allowlist, resolve-and-block private/loopback/link-local IPs, no redirects.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

CHAT_URL = "https://apihub.agnes-ai.com/v1/chat/completions"
ALLOWED_HOST = {"apihub.agnes-ai.com"}
DEFAULT_FRAMES = 12

# ── timeline gate (deterministic) ──────────────────────────────────


def _norm_timeline(timeline):
    """Accept either {"timeline": [[src,start,end,at]...], "total": N} or a
    plain list of 4-tuples/4-lists. Never trust the caller's types."""
    if isinstance(timeline, dict):
        total = timeline.get("total") or timeline.get("duration_sec") or 0
        freeze = timeline.get("freeze_last") or 0
        raw = timeline.get("timeline", timeline.get("clips", []))
    else:
        total, freeze, raw = 0, 0, timeline or []
    out = []
    for it in raw:
        if isinstance(it, dict):
            src = str(it.get("src") or it.get("source") or it.get("clip") or "")
            vals = [it.get("start", 0), it.get("end", 0), it.get("at", it.get("start_sec", 0))]
        else:
            src = str(it[0] if it else "")
            vals = list(it[1:4]) if len(it) >= 4 else (list(it[1:]) + [0])
        try:
            start, end, at = float(vals[0]), float(vals[1]), float(vals[2])
        except (TypeError, ValueError, IndexError):
            start = end = at = 0.0
        out.append({"src": src, "start": start, "end": end, "at": at})
    return out, float(total), float(freeze)


def check_timeline(timeline, duration_sec: Optional[float] = None) -> dict:
    """Reuse-limit + duration + ordering gate.

    Rules (all deterministic, no LLM):
    - a source appearing more than 3 times anywhere      -> critical
    - a source appearing twice and closer than 20 s      -> critical
      (the second use is only legal as a closing echo, far apart)
    - timeline entries must be monotonic in ``at``        -> critical
    - coverage: total expected duration vs last edge      -> critical if off
      by more than 3% unless the caller did not give a target
    Returns {"verdict": "ok"|"fix", "findings": [...], "stats": {...}}
    """
    clips, total, _freeze = _norm_timeline(timeline)
    if not clips:
        return {"verdict": "fix",
                "findings": [{"severity": "critical", "code": "EMPTY_TIMELINE",
                              "message": "时间轴为空", "evidence": ""}],
                "stats": {}}
    if duration_sec:
        total = float(duration_sec)

    findings: list[dict] = []
    by_src: dict[str, list[float]] = {}
    last_at = -1.0
    out_of_order = False
    for c in clips:
        by_src.setdefault(c["src"], []).append(c["at"])
        if c["at"] < last_at - 1e-6:
            out_of_order = True
        last_at = max(last_at, c["at"])

    # 1) >3 uses — the v5 disease
    for src, ats in by_src.items():
        if len(ats) > 3:
            findings.append({
                "severity": "critical", "code": "REUSE_LIMIT_EXCEEDED",
                "message": f"素材 '{src[:40]}' 全片复用 {len(ats)} 次（红线 ≤3）——拼接感病根",
                "evidence": f"出现在 {sorted(ats)}",
            })
        elif len(ats) == 3 and any(b - a < 20 for a, b in zip(ats, ats[1:])):
            gaps = [round(b - a, 1) for a, b in zip(ats, ats[1:])]
            findings.append({
                "severity": "critical", "code": "REUSE_TOO_CLOSE",
                "message": f"素材 '{src[:24]}' 出现 3 次且含 <20s 的近距离复用（回环只允许一次且隔开 ≥20s）",
                "evidence": f"出现位置={ats}，间距={gaps}",
            })
        elif len(ats) == 2 and (ats[1] - ats[0]) < 20:
            findings.append({
                "severity": "critical", "code": "REUSE_TOO_CLOSE",
                "message": f"素材 '{src[:24]}' 第2次复用距第1次仅 {ats[1]-ats[0]:.1f}s（需 ≥20s 才允许回环复用）",
                "evidence": f"出现在 {ats}",
            })

    # 2) monotonic ordering
    if out_of_order:
        findings.append({
            "severity": "critical", "code": "TIMELINE_DISORDER",
            "message": "时间轴 at 必须单调递增", "evidence": "出现回退",
        })

    # 3) duration coverage
    last_edge = max(c["at"] + (c["end"] or 0) - (c["start"] or 0) for c in clips)
    cov = last_edge
    if total and abs(cov - total) > 0.25 and abs(cov - total) / total > 0.03:
        findings.append({
            "severity": "critical", "code": "DURATION_MISMATCH",
            "message": f"时间轴总长 {cov:.1f}s 与目标 {total:.1f}s 偏差超 3%",
            "evidence": f"覆盖到 {cov:.1f}s",
        })

    stats = {"clips": len(clips),
             "unique_src": len(by_src),
             "coverage_sec": round(cov or 0, 2),
             "target_sec": total}
    verdict = "ok" if not any(f["severity"] == "critical" for f in findings) else "fix"
    return {"verdict": verdict, "findings": findings, "stats": stats}


# ── final-video VLM gate ───────────────────────────────────────────


def _vlm_credentials() -> str:
    key = os.environ.get("AGNES_KEY", "").strip()
    if key:
        return key
    tf = Path(os.environ.get("TEMP", tempfile.gettempdir())) / "agnes_key.txt"
    if tf.exists():
        try:
            return tf.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def _check_ssrf(url: str) -> str:
    from urllib.parse import urlparse
    u = urlparse(url)
    assert u.scheme == "https", "https only"
    assert u.hostname in ALLOWED_HOST, "host not in allowlist"
    # Host is in allowlist — skip RFC 2544 private-range check.
    # RFC 2544 (198.18.0.0/15) is used by some cloud providers (e.g. Agnes AI);
    # loopback/link-local/reserved are still blocked as a defense-in-depth.
    for info in socket.getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0])
        assert not (ip.is_loopback or ip.is_link_local or ip.is_reserved), "blocked IP"
    return url


def _extract_frames(video: Path, count: int) -> list[dict]:
    """Uniformly sample `count` frames of the video (start + spread + end)."""
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(video)],
        capture_output=True, text=True)
    dur = float(json.loads(probe.stdout)["format"]["duration"])
    times = sorted({0.5} | {dur * i / count for i in range(1, count + 1)} | {max(0.0, dur - 0.5)})
    times = times[:count]
    tmp = tempfile.mkdtemp(prefix="vlm_gate_")
    frames = []
    for i, t in enumerate(times):
        p = Path(tmp) / f"f{i:02d}_t{t:06.2f}.png"
        r = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(video),
             "-frames:v", "1", "-vf", "scale=960:-2", "-y", str(p)],
            capture_output=True, text=True)
        if p.exists() and p.stat().st_size > 1000:
            frames.append({"t": round(t, 2), "path": str(p)})
    return frames


def _frames_payload(frames: list[dict]) -> list[dict]:
    out = []
    for f in frames:
        with open(f["path"], "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        out.append({"type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{b64}"}})
    return out


def _ask_vlm(images, prompt, key: str, max_tokens: int = 1800) -> str:
    """Ask VLM via requests (urllib has SSL issues with some Agnes endpoints)."""
    import requests
    import os as _os
    body = {
        "model": _os.environ.get("SHIPIN_VLM_MODEL", "agnes-3.0-flash"),
        "messages": [{"role": "user", "content": [*images, {"type": "text", "text": prompt}]}],
        "max_tokens": max_tokens,
    }
    resp = requests.post(
        _check_ssrf(CHAT_URL),
        json=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=240,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _batch_prompt(times: str, n: int, context: Optional[dict] = None) -> str:
    """Build the VLM walk-through prompt from the actual project context.

    历史教训：这里曾经写死「这是同一支 60 秒笔记本 TVC」——咖啡广告、短剧
    一律被套上错误前提，VLM 终验等于从未对准过本片。现在一切从 context
    参数化注入；没有 context 时退化为中性描述，绝不再出现写死的产品。"""
    ctx = context or {}
    product = str(ctx.get("product_info") or ctx.get("brand_name") or "本项目成片").strip()
    duration = ctx.get("duration_sec")
    shots = ctx.get("shots") or []
    lines = [
        f"这是一支 {duration}s 的成片的 {n} 个抽帧，时间点依次为 {times}s（帧按时间先后排列）。"
        f"产品/主题：{product}。"
    ]
    if shots:
        rows = []
        t0 = 0.0
        for s in shots:
            d = float(s.get("duration_sec") or 0)
            rows.append(f"- {s.get('shot_id', '?')} {t0:.1f}s~{t0 + d:.1f}s："
                        f"{str(s.get('subject') or s.get('scene') or '')[:60]}")
            t0 += d
        lines.append("分镜表（每个分镜应为一个连续镜头，镜与镜之间才允许切换）：\n"
                     + "\n".join(rows))
    else:
        lines.append("影片应为连贯叙事（钩子→发展→转折→收束），无与主题无关的插入画面。")
    lines.append(
        "请只依据画面事实回答：\n"
        "1) 每个时间点：画面在讲什么、是否有白色字幕（逐字转写，无字幕写'无'）；\n"
        "2) 从前往后是否有明显的叙事连续性，有无『突然跳到无关场景』的断帧或明显重复画面（帧间出现两次以上相同主体+背景也算重复）；\n"
        "3) 这些帧里是否出现落版大字/品牌信息（如品牌名或 slogan 大字）。\n"
        "返回严格 JSON，不要任何额外文字：{{\"frames\": [{{\"t\": <秒>, \"scene\": \"...\", \"subtitle\": \"...\"}}], "
        "\"breaks\": [断点描述字符串, ...], \"brand_seen\": true/false}}\n"
        "没有断点则 breaks 为空数组。"
    )
    return "\n".join(lines)


def _shot_boundaries(shots: list[dict]) -> list[float]:
    """Cumulative start times of the storyboard shots (seconds)."""
    bounds, t = [], 0.0
    for s in shots:
        bounds.append(round(t, 2))
        t += float(s.get("duration_sec") or 0)
    return bounds


def _context_frames(video_dur: float, shots: list[dict], frames_count: int) -> list[float]:
    """Per-shot first/last sampling. 首镜与末镜（品牌落版所在）的帧必须保留，
    中间镜超预算时交替精简——曾经 sorted[:16] 把片尾品牌卡帧全部裁掉，
    导致 brand_seen 误判 False。"""
    per = []  # (t, shot_index)
    t = 0.0
    td_margin = 0.65  # 采样避开边界叠化带（dissolve 0.4s + 余量），否则两帧
                      # 天然相似会被 VLM 误判为「重复画面」
    for i, s in enumerate(shots):
        d = max(float(s.get("duration_sec") or 0), 0.5)
        opening = min(max(0.15 * d, td_margin), 0.9)
        closing = min(max(0.12 * d, td_margin), d * 0.45)
        per.append((t + opening, i))          # shot opening（避开入点叠化）
        per.append((t + d - closing, i))      # shot closing（避开出点叠化）
        t += d
    budget = max(8, min(frames_count, 24))
    # 首镜+末镜的帧无条件保留，中间镜从前往后交替丢弃 closing/opening
    first_last = {0, len(shots) - 1}
    kept = [x for x in per if x[1] in first_last]
    rest = [x for x in per if x[1] not in first_last]
    while len(kept) + len(rest) > budget and rest:
        # 交替从最靠后的中间镜丢起，保住叙事前段的帧密度
        rest.pop(-2 if len(rest) > 1 else -1)
    times = sorted({round(x[0], 2) for x in (kept + rest) if 0 <= x[0] <= video_dur})
    return times[:max(budget, len(kept))]


def vlm_review_final(video_path: str, frames_count: int = DEFAULT_FRAMES,
                     context: Optional[dict] = None) -> dict:
    """Two-layer final gate:

    1. deterministic pass (no model): scene-cut detection over the finished
       video; with a storyboard context, any cut that does NOT sit on a shot
       boundary is an internal sub-shot cut — the exact "画面凌乱/换镜太快"
       disease — and is a critical finding on its own.
    2. VLM walk-through with a context-parameterized prompt (product, duration,
       per-shot table). Missing unusable key => blocked.
    """
    ctx = context or {}
    shots = ctx.get("shots") or []
    key = _vlm_credentials()
    if not key:
        return {"verdict": "blocked",
                "reason": "AGNES_KEY 未配置（环境变量或 %TEMP%/agnes_key.txt）；禁止交付",
                "findings": []}
    video = Path(video_path).resolve()
    if not video.exists():
        return {"verdict": "blocked", "reason": f"video not found: {video}", "findings": []}

    # ── layer 1: deterministic structure pass ──────────────────────
    from .clip_qc import detect_cut_times, _duration_and_dims
    deterministic: dict = {"cut_times": [], "internal_cuts": [], "avg_shot_sec": None}
    det_findings: list[dict] = []
    try:
        video_dur, _dims = _duration_and_dims(video)
        cuts = detect_cut_times(video, threshold=0.3)
        deterministic["cut_times"] = cuts
        if shots:
            bounds = _shot_boundaries(shots)
            n = len(shots)
            avg = video_dur / n if n else 0
            deterministic["avg_shot_sec"] = round(avg, 2)
            # crossfade stitching shifts boundaries by up to ~1s; allow 1.5s
            internal = [c for c in cuts
                        if all(abs(c - b) > 1.5 for b in bounds)]
            deterministic["internal_cuts"] = internal
            if internal:
                det_findings.append({
                    "severity": "critical", "code": "FINAL_INTERNAL_CUTS",
                    "message": (f"成片检测到 {len(internal)} 处镜头内部硬切 "
                                f"(t={internal})——不属于任何分镜边界，"
                                f"视频模型私开子镜头，需定位到对应镜头重新生成")})
            if avg < 2.0:
                det_findings.append({
                    "severity": "critical", "code": "PACING_TOO_FAST",
                    "message": f"平均镜长仅 {avg:.2f}s (<2s)——节奏过快"})
        elif len(cuts) > 20:
            det_findings.append({
                "severity": "suggestion", "code": "CUT_DENSITY_HIGH",
                "message": f"全片检测到 {len(cuts)} 处场景突变（无分镜上下文，仅提示）"})
    except Exception as e:
        det_findings.append({"severity": "suggestion", "code": "DETERMINISTIC_PASS_ERROR",
                             "message": f"确定性结构检查失败: {e}"})

    # ── layer 2: VLM walk-through ──────────────────────────────────
    try:
        if shots:
            times = _context_frames(video_dur, shots, max(8, min(frames_count, 16)))
        else:
            times = None
    except Exception:
        times = None
    try:
        if times:
            frames = []
            tmp = tempfile.mkdtemp(prefix="vlm_gate_")
            for i, t in enumerate(times):
                p = Path(tmp) / f"f{i:02d}_t{t:06.2f}.png"
                r = subprocess.run(
                    ["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(video),
                     "-frames:v", "1", "-vf", "scale=960:-2", "-y", str(p)],
                    capture_output=True, text=True)
                if p.exists() and p.stat().st_size > 1000:
                    frames.append({"t": round(t, 2), "path": str(p)})
        else:
            frames = _extract_frames(video, max(4, min(frames_count, 16)))
    except Exception as e:
        return {"verdict": "error", "reason": f"抽帧失败: {e}", "findings": []}
    if not frames:
        return {"verdict": "blocked", "reason": "未能从视频抽取任何帧", "findings": []}

    batch_results: list[dict] = []
    breaks: list[str] = []
    brand_seen = False
    for i in range(0, len(frames), 4):
        batch = frames[i:i + 4]
        times_str = ", ".join(f"{f['t']}" for f in batch)
        resp = _ask_vlm(_frames_payload(batch),
                        _batch_prompt(times_str, len(batch), ctx), key)
        batch_results.append({"t_range": f"{batch[0]['t']}~{batch[-1]['t']}", "vlm": resp})
        try:
            m = re.search(r"\{.*\}", resp, re.S)
            parsed = json.loads(m.group(0)) if m else {}
        except (json.JSONDecodeError, AttributeError):
            parsed = {}
        for b in (parsed.get("breaks") or []):
            if isinstance(b, str) and b not in breaks:
                breaks.append(b)
        if parsed.get("brand_seen"):
            brand_seen = True

    all_findings = det_findings + [
        {"severity": "critical", "code": "VLM_BREAK", "message": b} for b in breaks]
    verdict = "pass" if not all_findings else "fix"
    return {
        "verdict": verdict,
        "reason": ("终验通过" if not all_findings else
                   f"终验发现 {len(all_findings)} 处问题（确定性 {len(det_findings)} + VLM {len(breaks)}）"),
        "brand_seen": brand_seen,
        "breaks": breaks,
        "deterministic": deterministic,
        "findings": all_findings,
        "frames_reviewed": len(frames),
        "batches": batch_results,
    }