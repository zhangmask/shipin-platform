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
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

CHAT_URL = "https://apihub.agnes-ai.com/v1/chat/completions"
ALLOWED_HOST = {"apihub.agnes-ai.com"}
DEFAULT_FRAMES = 12
# 每镜采样配额（帧/镜）：3 = 开-中-合四段全覆盖。成片时长越长帧数越多,
# 修改: 布的 '只抽查了某几帧不完整' 即源于 2 帧/镜的 '点检' 采样。
FRAMES_PER_SHOT = 3
FRAMES_PER_SHOT_MAX = 5
MAX_REVIEW_FRAMES = 64

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
            sid = str(it.get("shot_id") or "")
        else:
            src = str(it[0] if it else "")
            vals = list(it[1:4]) if len(it) >= 4 else (list(it[1:]) + [0])
            sid = ""
        try:
            start, end, at = float(vals[0]), float(vals[1]), float(vals[2])
        except (TypeError, ValueError, IndexError):
            start = end = at = 0.0
        out.append({"src": src, "start": start, "end": end, "at": at,
                    "shot_id": sid})
    return out, float(total), float(freeze)


def check_timeline(timeline, duration_sec: Optional[float] = None,
                   expected_shot_ids: Optional[list] = None) -> dict:
    """Reuse-limit + duration + ordering gate.

    Rules (all deterministic, no LLM):
    - a source appearing more than 3 times anywhere      -> critical
    - a source appearing twice and closer than 20 s      -> critical
      (the second use is only legal as a closing echo, far apart)
    - timeline entries must be monotonic in ``at``        -> critical
    - coverage: total expected duration vs last edge      -> critical if off
      by more than 3% unless the caller did not give a target
    - shot accounting: when ``expected_shot_ids`` is given, any scripted shot
      missing from the timeline (``SHOT_MISSING``) or any entry whose shot_id
      was not scripted (``SHOT_INJECTED``) is critical — a video cannot
      silently drop or smuggle shots.
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

    # 4) shot accounting (审计 C2):剧本镜号全集 vs 时间线实际镜号
    stats = {"clips": len(clips),
             "unique_src": len(by_src),
             "coverage_sec": round(cov or 0, 2),
             "target_sec": total}
    if expected_shot_ids:
        expected = [str(x) for x in expected_shot_ids if str(x).strip()]
        on_timeline = [c["shot_id"] for c in clips if c["shot_id"]]
        missing = sorted(set(expected) - set(on_timeline))
        injected = sorted(set(on_timeline) - set(expected))
        if missing:
            findings.append({
                "severity": "critical", "code": "SHOT_MISSING",
                "message": f"剧本有 {len(missing)} 个镜头未进入时间线: {missing}",
                "evidence": f"时间线实际镜号={on_timeline}"})
        if injected:
            findings.append({
                "severity": "critical", "code": "SHOT_INJECTED",
                "message": f"时间线含剧本未声明的镜头: {injected}",
                "evidence": f"时间线实际镜号={on_timeline}"})
        stats["expected_shots"] = len(expected)
        stats["on_timeline"] = on_timeline

    verdict = "ok" if not any(f["severity"] == "critical" for f in findings) else "fix"
    return {"verdict": verdict, "findings": findings, "stats": stats}


# ── 轮14:每镜旁白声轨存在性门(确定性,ASR-free) ──────────────────────
# 「符不符合剧本」此前只核视频:某镜 TTS 失败/音频错位时,画面照演但
# 嘴上没词,旧门只有全片 FINAL_NO_AUDIO 警告,抓不住单镜缺失。
# 按 align 的 narr_at 窗口用 silencedetect 核验:有旁白台词的镜,
# 其窗口内必须真有超过阈值的有声段。
_SIL_END_RE = re.compile(r"silence_end:\s*([0-9.]+)")
_NARR_MIN_SOUND_RATIO = 0.15   # 窗口内有声占比下限(低于=偏薄,警告)
_NARR_SILENCE_DB = -45.0       # 静音判定阈值(dB)


def _silence_spans(video: str, noise_db: float = _NARR_SILENCE_DB,
                   min_dur: float = 0.35) -> list[tuple[float, float]]:
    """ffmpeg silencedetect → [(start, end)] 静音段(秒)。never raises。"""
    try:
        r = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", str(video),
             "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}",
             "-f", "null", "-"],
            capture_output=True, text=True)
    except Exception:
        return []
    text = (r.stderr or "") + (r.stdout or "")
    starts = [float(m) for m in re.findall(
        r"silence_start:\s*([0-9.]+)", text)]
    ends = [float(m) for m in _SIL_END_RE.findall(text)]
    spans: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        # 个别构建在 EOF 不输出 silence_end——尾部静音延到大值兜底,
        # 否则 [start, start+d] 之外的有声会被漏算成"有旁白"
        e = ends[i] if i < len(ends) else s + 3600.0
        spans.append((s, e))
    return spans


def check_narration_presence(video: str, shots: list[dict]) -> dict:
    """每镜旁白声轨存在性(轮14,确定性,无 ASR/模型依赖)。

    shots 每项需含 {shot_id, narration?, dialogue?, narr_at|
    audio_start_sec, tts_sec?, duration_sec(align 窗口)};narration 与
    dialogue(字符串或 {text, role} 字典)任一非空且起点可解析的镜才查
    (轮20:纯台词镜也要查)——都没有台词的纯画面镜(手冲特写/logo
    落版)不要求有声。判定:
      - 旁白窗口 [at, at+tts_sec] 整体落在静音段内 → critical
        NARRATION_MISSING(TTS 缺失/音频错位:画面照演,嘴上没词);
      - 有声占比 < _NARR_MIN_SOUND_RATIO → warning NARRATION_THIN;
      - 成片无任何音轨 → critical NO_AUDIO_TRACK(整轨缺失)。
    返回 {"verdict": "ok"|"fix", "findings": [...], "stats": {...}}。
    """
    findings: list[dict] = []
    checked = 0
    try:
        has_audio = _has_audio_stream(video)
    except Exception:
        has_audio = True  # 探测失败不误判,交由既有 FINAL_NO_AUDIO 门
    if not has_audio:
        return {"verdict": "fix",
                "findings": [{"severity": "critical", "code": "NO_AUDIO_TRACK",
                              "message": "成片无音轨——旁白/氛围声全部缺失,"
                                         "音画审查不可用"}],
                "stats": {"checked": 0}}
    spans = _silence_spans(video)

    def _sounding(a: float, b: float) -> float:
        """[a,b] 内非静音时长。"""
        if b <= a:
            return 0.0
        silent = 0.0
        for s, e in spans:
            lo, hi = max(s, a), min(e, b)
            if hi > lo:
                silent += hi - lo
        return (b - a) - silent

    for s in shots:
        if not isinstance(s, dict):
            continue
        # 轮20:台词镜(narration 为空、只有 dialogue)也要查——剧本要求
        # 「至少 2 镜必须有 dialogue」,这些镜的 TTS 失败此前整镜跳过
        narration = str(s.get("narration") or "").strip()
        _dlg = s.get("dialogue")
        dialogue = (str(_dlg.get("text") or "").strip()
                    if isinstance(_dlg, dict) else str(_dlg or "").strip())
        text = narration or dialogue
        kind = "旁白" if narration else "台词"
        if not text:
            continue
        # align 时间轴两种历史字段名都认:narr_at(新) / audio_start_sec(旧)
        raw_at = s.get("narr_at")
        if raw_at is None:
            raw_at = s.get("audio_start_sec")
        try:
            at = float(raw_at)
        except (TypeError, ValueError):
            continue
        # 窗口优先 tts_sec(该镜旁白真实时长),缺省回退 align 窗口
        try:
            tts = float(s.get("tts_sec") or 0)
        except (TypeError, ValueError):
            tts = 0.0
        win = tts if tts > 0.2 else (float(s.get("duration_sec") or 0) or 3.0)
        a, b = max(at, 0.0), max(at, 0.0) + min(win, 10.0)
        checked += 1
        sound = _sounding(a, b)
        ratio = sound / (b - a) if b > a else 0.0
        sid = s.get("shot_id", "?")
        if sound <= 0.05:
            findings.append({
                "severity": "critical", "code": "NARRATION_MISSING",
                "message": (f"镜头{sid} {kind}窗口 {a:.2f}~{b:.2f}s 全程静音——"
                            f"TTS 缺失或音频错位(画面照演,嘴上没词): "
                            f"{text[:40]}")})
        elif ratio < _NARR_MIN_SOUND_RATIO:
            findings.append({
                "severity": "warning", "code": "NARRATION_THIN",
                "message": (f"镜头{sid} {kind}窗口 {a:.2f}~{b:.2f}s 有声占比 "
                            f"仅 {ratio:.0%}(阈值 {_NARR_MIN_SOUND_RATIO:.0%})"
                            f"——旁白可能被截断/音量过低")})
    verdict = "ok" if not any(f["severity"] == "critical"
                              for f in findings) else "fix"
    return {"verdict": verdict, "findings": findings,
            "stats": {"checked": checked}}


def _has_audio_stream(video: str) -> bool:
    """ffprobe 探测是否存在音轨(never raises)。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0",
             str(video)], capture_output=True, text=True)
        return bool((r.stdout or "").strip())
    except Exception:
        return True


# ── 轮21:关键帧图 vs 分镜文本(视频生成前的第一道画面门) ─────────────
# qc_clip 的 dHash 只比「clip 首帧 vs 参考图」——同源,几乎必然一致;
# 而视频模型以 first_frame 为条件生成,关键帧跑偏则整镜必歪,且要到
# 视频阶段甚至终审才发现(返工最贵的一环)。在 generate 里、视频生成
# 前先把关键帧对照分镜主体/场景文本审掉。


def vlm_image_matches_text(image_path: str, expect: dict, key: str) -> dict:
    """问 VLM 关键帧图是否体现分镜文本(主体/场景)。never raises。

    信息不足无法判断时 match=true(不冤控)——宁可漏报不可误杀,
    误杀的代价是无限重生成。"""
    if not key:
        return {"available": False, "match": None,
                "reason": "AGNES_KEY 未配置"}
    p = Path(image_path)
    if not p.is_file():
        return {"available": False, "match": None, "reason": "关键帧不存在"}
    subj = str(expect.get("subject") or "")[:60]
    scene = str(expect.get("scene") or "")[:40]
    if not subj and not scene:
        return {"available": True, "match": True,
                "reason": "无文本预期,跳过"}
    try:
        with open(p, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        prompt = (
            "下图是广告分镜的首帧关键图。只依据画面事实判断它是否体现了"
            f"分镜的文字描述——主体[{subj}]、场景[{scene}]。"
            "主体身份/场景类型不符即不符(构图/光线/景别差异不算);"
            "信息不足无法判断时 match 给 true。"
            '返回严格 JSON：{"match": true/false, "reason": "..."}'
        )
        resp = _ask_vlm(
            [{"type": "image_url",
              "image_url": {"url": "data:image/png;base64," + b64}}],
            prompt, key, max_tokens=300)
        m = re.search(r"\{.*\}", resp, re.S)
        parsed = json.loads(m.group(0)) if m else {}
        # 与轮11 _same_person 同一教训:载荷没有 match 字段(协议错配/
        # 路由错/JSON 截断)→ available=False 跳过,绝不让「解析失败」
        # 冒充「不符」的 critical——一次 hiccup 不该拦掉整镜
        if "match" not in parsed:
            return {"available": False, "match": None,
                    "reason": "VLM 未返回 match 字段(载荷不匹配),跳过本次审图"}
        return {"available": True, "match": bool(parsed.get("match")),
                "reason": str(parsed.get("reason", ""))[:200]}
    except Exception as e:
        return {"available": False, "match": None,
                "reason": f"VLM 关键帧审图失败: {type(e).__name__}: {e}"[:200]}


def check_keyframes(shots: list[dict], key: Optional[str] = None) -> dict:
    """逐镜关键帧 vs 分镜文本(轮21)。

    shots: [{shot_id, first_frame, subject, scene}];
    返回 {"verdict": "ok"|"fix", "findings": [...], "stats": {...}}。
    finding 带 shot_id 字段,critical 由调用方并入 shots_review 既有
    合并通道(终审统一阻断)。"""
    key = key or _vlm_credentials()
    findings: list[dict] = []
    checked, skipped = 0, 0
    if not key:
        return {"verdict": "ok", "findings": [], "stats": {"checked": 0},
                "reason": "AGNES_KEY 未配置，跳过关键帧审图"}
    for s in shots:
        if not isinstance(s, dict):
            continue
        img = s.get("first_frame")
        subj = str(s.get("subject") or "").strip()
        scene = str(s.get("scene") or "").strip()
        if not img or not Path(img).is_file() or not (subj or scene):
            skipped += 1
            continue
        r = vlm_image_matches_text(img, s, key)
        if not r.get("available"):
            continue
        checked += 1
        if not r.get("match"):
            findings.append({
                "shot_id": s.get("shot_id"),
                "severity": "critical", "code": "KEYFRAME_MISMATCH",
                "message": (f"镜头{s.get('shot_id', '?')} 关键帧与分镜文本不符"
                            f"(主体[{subj[:24]}] 场景[{scene[:16]}]): "
                            f"{r.get('reason', '')[:100]}——视频以该帧为条件"
                            f"生成必歪,应重新生成关键帧")})
    return {"verdict": "ok" if not findings else "fix",
            "findings": findings,
            "stats": {"checked": checked, "skipped": skipped}}


# ── final-video VLM gate ───────────────────────────────────────────


def _vlm_credentials() -> str:
    """Resolve AGNES key from env FIRST; the %TEMP%/agnes_key.txt fallback only
    as a legacy convenience. 2026-09-21 实跑事故：temp 文件曾被外部进程写成
    模型返回文本(『11.09|从咖啡店门口…』形态)，被放进 Authorization 头导致
    latin-1 UnicodeEncodeError——key 只信任 env/.env，且必须通过形状校验。
    """
    key = os.environ.get("AGNES_KEY", "").strip()
    if _key_ok(key):
        return key
    tf = Path(os.environ.get("TEMP", tempfile.gettempdir())) / "agnes_key.txt"
    if tf.exists():
        try:
            key = tf.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        if _key_ok(key):
            return key
    return ""


def _key_ok(key: str) -> bool:
    """机器密钥形状校验：仅 ASCII [A-Za-z0-9._=-]，且长度 ≥16——把
    「agent 返回文本被误当 key」这类事故挡在发送之前（否则非 ASCII
    进 HTTP 头直接 latin-1 崩）。"""
    if not key or len(key) < 16:
        return False
    return all(32 < ord(c) < 127 for c in key)


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


def _extract_frames(video: Path, count: int) -> tuple[list[dict], str]:
    """Uniformly sample `count` frames of the video (start + spread + end).

    轮19:返回 (frames, tmp_dir)——调用方须在批次编码完成后
    `_cleanup_tmp(tmp_dir)`,否则每个审查调用泄漏一个含 N 张 960px
    PNG 的目录(磁盘打满事故的根因之一)。"""
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
    return frames, tmp


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
    if not _key_ok(key):  # 发送前保险:坏 key 平缓报错,不把垃圾写进 Authorization 头
        raise ValueError("AGENT_KEY 校验失败(长度<16 或含非 ASCII)——禁止把非密钥内容当凭据上送")
    body = {
        "model": _os.environ.get("SHIPIN_VLM_MODEL", "agnes-3.0-flash"),
        "messages": [{"role": "user", "content": [*images, {"type": "text", "text": prompt}]}],
        "max_tokens": max_tokens,
    }
    # 上游偶发 5xx/网络瞬断(实测:真实终片审查批量请求见过 500)——重试 3 次
    # 退避 3s,别让一次瞬态错误毁掉整场终验(失败仍会如实抛出)。
    last_exc: Optional[Exception] = None
    for _attempt in range(3):
        try:
            resp = requests.post(
                _check_ssrf(CHAT_URL),
                json=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=240,
            )
            if resp.status_code < 500:
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except (requests.exceptions.RequestException, KeyError, ValueError) as e:
            last_exc = e
        time.sleep(3 * (_attempt + 1))
    raise RuntimeError(f"AGNES VLM 请求 3 次均失败: {last_exc}")


# ── cross-shot identity gate (审计:A.4 模板,VBench-2.0 human_identity 思路) ─


SAME_PERSON_PROMPT = (
    "图1、图2 是同一部影片中先后两个镜头里的人物。仅依据画面事实判断："
    "是否为同一人（同一张脸，同一发型；着装有 90% 以上一致可接受微差）。"
    "注意：构图、景别、光线差异不考虑；换人、换发型、换服装颜色/款型均为不一致。"
    '返回严格 JSON：{"same": true/false, "spec": "<不一致的点：脸/发型/服装/身材>", "reason": "..."}'
)


def _capture(video: Path, t: float) -> str:
    """抽一帧图存临时文件,返回路径(失败返回空串)。

    轮19:目录由调用方(_compare_person)负责删除——此前每帧一个
    mkdtemp 只建不清,长跑服务把 TEMP 打满(2540 个目录/2.7G 实证)。"""
    tmp = tempfile.mkdtemp(prefix="vlm_identity_")
    p = Path(tmp) / f"f_{t:06.2f}.png"
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video),
         "-frames:v", "1", "-vf", "scale=960:-2", "-y", str(p)],
        capture_output=True, text=True)
    return str(p) if p.exists() and p.stat().st_size > 1000 else ""


def _subject_tokens(subject: str) -> set:
    """主体描述里的判别性词元:两位及以上连续汉字,或常见人物单字词。"""
    han = re.findall(r"[\u4e00-\u9fff]{2,}", subject or "")
    han += [ch for ch in (subject or "") if ch in "他她女男"] * 2
    return set(han)


def _same_person(imgs: list, key: str) -> dict:
    """VLM 成对判定是否为同一人。never raises.

    轮11a 修正(实测教训):响应里没有 "same" 字段——协议错配、载荷被路由到
    别的提示词、JSON 截断——一律判 available=False 跳过。绝不让「解析失败」
    冒充「不是同一人」的 critical 判决:那会让一次 VLM  hiccup 直接变成
    「镜内身份已更换,禁止交付」,协议错误不该阻断交付。"""
    try:
        resp = _ask_vlm(imgs, SAME_PERSON_PROMPT, key, max_tokens=300)
        m = re.search(r"\{.*\}", resp, re.S)
        parsed = json.loads(m.group(0)) if m else {}
        if "same" not in parsed:
            return {"available": False, "same": None,
                    "reason": "VLM 未返回 same 字段(载荷不匹配),跳过本次判定"}
        # 轮40:字段漂移容忍——模型偶尔用 difference/points 代替 spec
        # (六审 #4:纯命名漂移会让轮35c 的"spec 空→critical"误杀正常片);
        # 取首个非空字段,整句缺失仍走 available=False 的 skip 通道。
        _spec = (parsed.get("spec") or parsed.get("difference")
                 or parsed.get("points") or "")
        return {"available": True, "same": bool(parsed.get("same")),
                "spec": str(_spec)[:80],
                "reason": str(parsed.get("reason", ""))[:200]}
    except Exception as e:
        return {"available": False, "same": None,
                "reason": f"VLM 跨镜身份判定失败: {type(e).__name__}: {e}"[:200]}


# ── 轮11a(2026-09-21):镜内人物一致性判定所需的「人物镜头」识别 ─────────
# 只有主体描述指向「人」的镜头才做首末帧对比:手冲特写、logo 落版等
# 无人物画面交给 VLM 判"是否同一人"只会得到无意义结论。
# 轮16:补 男女/职业词——"男生在加班""程序员坐下"这类主体此前不在表内,
# 整镜的身份判定被静默跳过。只用复合词不用单字"男/女":「女包」
# 「男装」等商品镜不该被当人物镜。
# 轮18:补代词"她/她们"——LLM 写「她坐在窗边」这类主体(无"主角"
# 字样)此前两个通道全跳过。「她」无商品词包含,可安全用单字;
# 「他」不行——「其他」会误命中。
_PERSON_HINTS = ("主角", "主人公", "人物", "模特",
                 "她", "她们",
                 "女子", "女人", "女性", "女孩", "女生", "男女",
                 "男子", "男人", "男性", "男生",
                 "咖啡师", "店员", "顾客", "消费者", "用户",
                 "老人", "孩子", "少年", "青年", "大学生", "学生",
                 "上班族", "白领", "程序员", "设计师", "博主",
                 "创业者", "主播", "演员", "舞者", "歌手", "厨师",
                 "司机", "医生", "教师", "主持人", "嘉宾")

# 轮16:具体角色词——相邻两镜各自声明了**不同**角色时不做身份判定
# (「顾客」vs「店员」是分镜本意的合理切换);泛称(主角/主人公…)不在此列
_PERSON_ROLES = ("顾客", "店员", "咖啡师", "消费者", "用户",
                 "老人", "孩子", "少年", "大学生", "学生",
                 "女子", "女人", "女性", "女孩", "女生",
                 "男子", "男人", "男性", "男生",
                 "上班族", "白领", "程序员", "设计师", "博主",
                 "创业者", "主播", "演员", "舞者", "歌手", "厨师",
                 "司机", "医生", "教师", "主持人", "嘉宾", "模特")


def _is_person_shot(subject: str) -> bool:
    return any(h in str(subject or "") for h in _PERSON_HINTS)


# 轮17:剧本钉外观判定——anchor/任一镜主体写了服装发型具体式样时,
# 跨镜换装即"违反剧本"(不是风格选择),COSTUME_SWAP 升 critical。
# 「镜」「装」这类单字不收(「镜头」「装饰」会误命中)。
_APPEARANCE_WORDS = ("衫", "外套", "大衣", "围巾", "裙", "西装", "制服",
                     "毛衣", "T恤", "恤", "裤", "帽", "鞋", "眼镜", "发型",
                     "长发", "短发", "直发", "卷发", "披肩", "马尾", "胡须")


def _look_pinned(shots: list[dict], actor_anchor: str = "") -> bool:
    if any(w in str(actor_anchor or "") for w in _APPEARANCE_WORDS):
        return True
    for s in shots:
        if isinstance(s, dict) and any(
                w in str(s.get("subject") or "") for w in _APPEARANCE_WORDS):
            return True
    return False


# 轮24:角色同义词归一化——「白领在办公」vs「上班族在地铁」本是同一类
# 人,字面不同会被误判成"不同角色"而整对跳过,身份从不比对。先归一到
# 规范角色再比交集;顾客vs店员这类真不同角色仍跳过。
_ROLE_SYNONYMS = {
    "女子": "女性", "女人": "女性", "女孩": "女性", "女生": "女性",
    "男子": "男性", "男人": "男性", "男孩": "男性",
    "消费者": "顾客", "用户": "顾客",
    "孩子": "儿童", "少年": "儿童",
    "白领": "上班族",
}


def _roles_of(subject: str) -> set:
    """主体里声明了的规范角色集合(同义词已归一)。"""
    return {_ROLE_SYNONYMS.get(r, r)
            for r in _PERSON_ROLES if r in str(subject or "")}


def _person_pair(a: dict, b: dict) -> bool:
    """相邻两镜是否应做跨镜身份判定(轮16 重写配对判据)。

    旧规则只认主体词元精确重叠——「主角端起咖啡杯」vs「主角」这类
    同人不同写的相邻镜被跳过(coffee-v7 实证:S05→S05b 正是换人的
    镜界,却从未进过跨镜门,只有镜内通道兜到)。新规则:
      1) 两镜都是人物镜(_PERSON_HINTS);
      2) 不构成「不同角色」——双方各自声明了无交集的规范角色时跳过
        (顾客vs店员的合理切换不误判;轮24:白领vs上班族经同义词归一
        后同角色,比);一方或双方只用泛称(主角/主人公…) → 判定为
        同一主人公,比。
    """
    sa, sb = str(a.get("subject") or ""), str(b.get("subject") or "")
    if not _is_person_shot(sa) or not _is_person_shot(sb):
        return False
    ta, tb = _subject_tokens(sa), _subject_tokens(sb)
    if ta & tb:
        return True  # 旧规则:词元重叠(同一描述的重复写法)
    roles_a = _roles_of(sa)
    roles_b = _roles_of(sb)
    if roles_a and roles_b and not (roles_a & roles_b):
        return False  # 双方明确不同角色(顾客vs店员)
    return True


def _compare_person(video: Path, ta: float, tb: float, key: str) -> dict:
    """抽 ta/tb 两帧问 VLM 是否同一人。返回 {"captured", "available",
    "result"};抽帧失败时 captured=False(调用方不计入 checked)。

    轮19:两帧的临时目录在本函数内删除(编码进 base64 后即无用)。"""
    pa, pb = _capture(video, ta), _capture(video, tb)
    if not pa or not pb:
        _cleanup_tmp(Path(pa).parent if pa else None,
                     Path(pb).parent if pb else None)
        return {"captured": False, "available": False, "result": {}}
    try:
        imgs = []
        for fp in (pa, pb):
            with open(fp, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode()
            imgs.append({"type": "image_url",
                         "image_url": {"url": "data:image/png;base64," + b64}})
        r = _same_person(imgs, key)
        return {"captured": True, "available": bool(r.get("available")),
                "result": r}
    finally:
        _cleanup_tmp(Path(pa).parent, Path(pb).parent)


def _identity_finding(a_label: str, b_label: str, r: dict, intra: bool,
                      pinned_look: bool = False) -> dict:
    """换人/换装 finding;intra=True 时为镜头内部(首帧 vs 末帧)判定。

    轮17:pinned_look(剧本钉了服装/发型式样)时换装也升 critical——
    脚本写死了造型,跨镜换装就是违反剧本,不是风格选择。
    轮35:spec 空(VLM 只说"不是同一人"、没给不一致点)时向 critical
    兜底——旧逻辑 is_face=False → COSTUME_SWAP warning,而单镜诊断的
    warning 会被 assemble 的 critical 过滤器直接丢弃,"换人"在模型
    措辞不利时静默降级(五审 #4)。
    轮41:spec 与 reason 双空(模型既不给不一致点也不解释)时不再直接
    critical,降为 IDENTITY_UNVERIFIED suggestion——最没信息量的答案
    不该受最重的罚(六审 #4 残留);reason 有实质内容(如"脸部完全
    不同")仍走 critical 兜底。"""
    spec = str(r.get("spec") or "")
    reason = str(r.get("reason") or "")
    if not spec and not reason.strip():
        return {
            "severity": "suggestion", "code": "IDENTITY_UNVERIFIED",
            "scope": "intra" if intra else "cross",
            "message": (f"镜头{a_label} 内部(首帧 vs 末帧)" if intra
                        else f"镜头{a_label}→镜头{b_label}"
                        ) + " VLM 判定不是同一人但未给出任何不一致点或解释"
                          "——身份存疑但证据不足,建议人工复核或重审"}
    is_face = ("脸" in spec or "发" in spec) or not spec
    is_critical = is_face or (pinned_look and _spec_is_costume(spec))
    where = ("镜头%s 内部(首帧 vs 末帧)" % a_label if intra
             else "镜头%s→镜头%s" % (a_label, b_label))
    return {
        "severity": "critical" if is_critical else "warning",
        "code": "IDENTITY_SWITCH" if is_face else "COSTUME_SWAP",
        "scope": "intra" if intra else "cross",
        "message": (f"{where} VLM 判定不是同一人"
                    f"(不一致点:{spec or reason[:60]})——"
                    f"{'镜内' if intra else '跨镜'}身份"
                    f"{'已更换,禁止交付' if is_critical else '被换装,需复核'}"
                    + ("（剧本已钉死人物造型,换装即违反剧本）"
                       if pinned_look and _spec_is_costume(spec)
                       else ""))}


def _spec_is_costume(spec: str) -> bool:
    """不一致点是否只是服装层(不含脸/发)——后者一律 critical。"""
    return bool(spec) and not ("脸" in spec or "发" in spec)


def _identity_gate(video: Path, shots: list[dict], key: str,
                   pinned_look: bool = False) -> dict:
    """人物一致性双通道:
    1) 跨镜:相邻两镜按 _person_pair 判定是否需要比(都是人物镜且不
       构成不同角色即比——「顾客」vs「店员」的合理切换仍跳过,
       「主角端起咖啡杯」vs「主角」的同人不同写不再被漏掉)。
       每对镜各抽中帧问 VLM;
    2) 镜内(轮11a):主体为「人」且时长≥1.5s 的镜头,抽首帧(15%)与末帧
       (85%)对比——coffee-v7 实测 S02 在 t=4.03s 镜内换装、S05b 窗口
       头部换人,这类同一镜头内部的更换此前只能靠跨镜中帧间接撞见且
       归属错位,现在直接钉在该镜上。
    只有在 VLM 判定"不是同一人"时输出 findings。审计盲区①(A 节)的落地。"""
    pairs, checked, findings = [], 0, []
    for i in range(len(shots) - 1):
        a, b = shots[i], shots[i + 1]
        if not isinstance(a, dict) or not isinstance(b, dict):
            continue
        # 轮16:配对判据从「主体词元精确重叠」升级为 _person_pair——
        # 同人不同写(「主角端起咖啡杯」vs「主角」)的相邻镜必须比,
        # 明确不同角色(顾客vs店员)仍跳过
        if _person_pair(a, b):
            pairs.append((i, i + 1))
    for i, j in pairs:
        a, b = shots[i], shots[j]
        # 两镜的镜头中帧(绝对时间):镜头起点=此前所有窗口时长之和
        t0a = sum(float(s.get("duration_sec") or 0) for s in shots[:i]
                  if isinstance(s, dict))
        da = float(a.get("duration_sec") or 0)
        cmp = _compare_person(video, t0a + da / 2,
                              t0a + da + float(b.get("duration_sec") or 0) / 2,
                              key)
        if not cmp["captured"]:
            continue
        checked += 1
        r = cmp["result"]
        if not cmp["available"]:
            continue
        if not r["same"]:
            findings.append(_identity_finding(
                str(a.get("shot_id", i + 1)),
                str(b.get("shot_id", j + 1)), r, False, pinned_look))
    # ── 镜内首/末帧对比(轮11a) ─────────────────────────────────────
    intra_pairs, intra_checked = [], 0
    for i, s in enumerate(shots):
        if not isinstance(s, dict):
            continue
        dur = float(s.get("duration_sec") or 0)
        if dur < 1.5 or not _is_person_shot(s.get("subject")):
            continue
        t0 = sum(float(x.get("duration_sec") or 0) for x in shots[:i]
                 if isinstance(x, dict))
        cmp = _compare_person(video, t0 + dur * 0.15, t0 + dur * 0.85, key)
        if not cmp["captured"]:
            continue
        intra_pairs.append(i)
        intra_checked += 1
        r = cmp["result"]
        if not cmp["available"]:
            continue
        if not r["same"]:
            findings.append(_identity_finding(
                str(s.get("shot_id", i + 1)), "", r, True, pinned_look))
    return {"pairs": pairs, "checked": checked, "findings": findings,
            "intra_pairs": intra_pairs, "intra_checked": intra_checked}


def _batch_prompt(times: str, n: int, context: Optional[dict] = None,
                  batch: Optional[list[dict]] = None) -> str:
    """Build the VLM walk-through prompt from the actual project context.

    历史教训：这里曾写死「这是同一支 60 秒笔记本 TVC」——咖啡广告、短剧
    一律被套上错误前提，VLM 终验从未对准过本片。现在 context 参数化注入，
    且 batch 携带每帧所属镜头（shot 标签），VLM 据镜头归属检查连续性，
    避免「只见帧序、不知镜头」导致把同镜头采样帧误判为断点。"""
    from copy import deepcopy
    ctx = deepcopy(context or {})
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
        lines.append("分镜表（每个分镜应为一个连续镜头，镜与镜之间才允许切换）："
                     + "\n".join(rows))
    else:
        lines.append("影片应为连贯叙事（钩子→发展→转折→收束），无与主题无关的插入画面。")
    if batch:
        shot_seq = ", ".join(f"{f.get('t')}s→{f.get('shot', '?')}" for f in batch)
        lines.append(f"这批抽帧的镜头归属：{shot_seq}（归属相同 = 同一镜头内的多个时间点）。")
    # 分镜符合度(G4):每帧给出其分镜预期的场景/主体/动作,让 VLM 逐帧核对
    # 「画面是否真的在演剧本写的那一幕」——审计 G3 证明旧提示词(只说"镜头归属"
    # 与"叙事连续")抓不住「画面与分镜不符」:模型生成的是另一幕却被放行。
    if batch and shots:
        expect = []
        for f in batch:
            s = shots[f.get("shot_idx") or 0] if f.get("shot_idx") is not None else None
            if not s:
                continue
            exp = (f"{str(s.get('scene') or '')[:36]}".strip() or "—")
            subj = (f"{str(s.get('subject') or '')[:24]}".strip() or "—")
            mo = (f"{str(s.get('motion') or '')[:24]}".strip() or "—")
            expect.append(f"  t={f.get('t')}s 镜{s.get('shot_id', '?')} 预期："
                          f"场景[{exp}] 主体[{subj}] 动作[{mo}]")
        if expect:
            lines.append("分镜预期（逐帧必须与所属分镜的这一行一致，不一致就是『演错剧本』）："
                         + "\n".join(expect))
    lines.append(
        "请只依据画面事实回答，核对每帧是否与其镜头归属一致、同镜头内部各帧是否前后衔接自然：\n"
        "1) 每个时间点的画面在讲什么、是否有白色字幕（逐字转写，无字幕写'无'）；\n"
        "2) 从前往后叙事是否连续，有无『突然跳到无关场景』、『同一镜头内画面突变/主体变形』、"
        "『明显重复画面（两次以上相同主体+背景）』；注意力在镜头中段的抽帧上——那里最容易出现"
        "画面崩坏（五官变形、多指、结构扭曲）；\n"
        "3) 这些帧里是否出现落版大字/品牌信息（如品牌名或 slogan 大字）；\n"
        "4) 对照『分镜预期』逐帧核对：画面里的场景/主体/动作与该镜剧本预期不符的帧，"
        "必须列入 shot_issues（如分镜写『办公室』画面却是厨房、分镜主体是『女主角』"
        "画面却出现其他主体）；能对上就不要写。\n"
        "返回严格 JSON，不要任何额外文字：{\"frames\": [{\"t\": <秒>, \"scene\": \"...\", "
        "\"subtitle\": \"...\", \"anomaly\": 0/1}], "
        "\"breaks\": [{\"t\": <秒>, \"desc\": \"...\", \"kind\": \"intra\"|\"boundary\"}], "
        "\"brand_seen\": true/false,"
        " \"shot_issues\": [{\"shot\": \"S03\", \"issue\": \"...\"}]}\n"
        "断帧规则：同一分镜内部的突变/崩坏/闪帧才算异常，kind='intra' 且必须列入 breaks；"
        "相邻分镜交界处的正常画面切换（t 落在进分镜表给出的镜头边界附近，"
        "只是『换了一个镜头』）必须标 kind='boundary'，不得列入异常——"
        "没有 intra 断点则 breaks 为空数组；"
        "有内容崩坏（变形/漂移/断帧）的帧在 anomaly 标记 1；"
        "所有帧都符合分镜预期则 shot_issues 为空数组。"
    )
    _bname = str(ctx.get("brand_name") or "").strip()
    if _bname:
        lines.append(f"提示：本片声明的品牌名是「{_bname}」——brand_seen 只在该文字"
                     f"（或含该名称的落版大字）真实出现在画面中时才为 true，"
                     f"其他品牌的字样不算。")
    return "\n".join(lines)


def _shot_boundaries(shots: list[dict], with_total: bool = False) -> list[float]:
    """Cumulative start times of the storyboard shots (seconds).

    with_total=True 时末尾追加 Σ(末镜结束时刻)——轮40:它是"片尾边界
    锚点",让末镜起点边界的右侧豁免窗按末镜时长定尺(短末镜不再被左邻
    长镜的窗口吞掉,六审 #2),尾段帧也能相对 Σ 判定不在豁免窗内。
    默认 False:确定性滤波(内部切/瞬变/黑帧的"临近边界=合法转场"语义)
    要保持旧行为——Σ 不是画面里的真实剪辑点(尤其是还有尾段时),把
    临近片尾的闪帧/黑屏当合法过渡会放行真缺陷。"""
    bounds, t = [], 0.0
    for s in shots:
        bounds.append(round(t, 2))
        t += float(s.get("duration_sec") or 0)
    if with_total and bounds:
        bounds.append(round(t, 2))
    return bounds


# ── 轮10a(2026-09-21):kind 缺失时的语义兜底 ────────────────────────────
# agnes-3.0-flash 对同一批断帧的 kind 标注跨会话不稳定(实测同批 6 条
# break,一轮全带 kind、下一轮可能全不带),缺 kind 时合法换镜回落成
# "intra" 被误拦。兜底:换镜语义词 + 排除告警词,缺一不可——
# 真实缺陷(29.18s"…疑似画面内容错位")同时含"切换"与"错位",告警词
# 优先 → 仍按 critical 拦截。


def _cleanup_tmp(*dirs) -> None:
    """删除抽帧/身份判定产生的临时目录。never raises。

    轮19(磁盘打满事故):此前每帧一个 mkdtemp 只建不清——真实审查
    一晚泄漏 2540 个目录/2.7G,TEMP 盘 100% 满后 E2E 直接失败。
    服务长跑场景下这是必修项。"""
    for d in dirs:
        if not d:
            continue
        shutil.rmtree(str(d), ignore_errors=True)


_BOUNDARY_MARGIN = 2.0  # 采样帧距剪辑瞬间 0.45~1.6s,跨镜对帧最远 ~1.6s
# 轮35/40:单侧豁免窗 = min(margin, 1.6s 采样覆盖保底, 该侧相邻镜长)。
# 原固定 2.0s 对 ≤4s 短镜覆盖整镜(bounds=[0,4] 时任意 t 都 |t-b|≤2),
# 中段崩坏标 kind=boundary 即被静默豁免(五审 #1 实锤:4s 镜 t=1.0~3.9
# 全 True)。1.6s 保底来自 VLM 报的是采样秒不是剪辑瞬间(最远 ~1.6s),
# 短镜的合法边界采样(如 2s 镜 t=1.4)必须仍能豁免;按"该侧镜长"封顶后
# 4s 镜单侧窗 1.6s(中段 1.6~2.4 不再被吞),末镜右侧由 Σ 锚点按末镜
# 时长定尺(六审 #2:短末镜不再被左邻长镜的窗口吞掉)。
_BOUNDARY_MIN_COVER = 1.6
_BOUNDARY_SWITCH_WORDS = ("切换", "换镜", "转场", "镜头交替", "切至",
                          "镜头切换", "跨镜头", "正常交接", "过渡")
_BOUNDARY_ALARM_WORDS = ("错位", "异常", "疑似", "崩坏", "花屏", "变形",
                         "漂移", "鬼影", "残影", "撕裂", "闪烁", "闪白",
                         "闪帧", "雪花", "污染", "缺损", "丢失")


def _is_boundary_transition(t_b: Optional[float], kind: str, desc: str,
                            bounds: list[float],
                            margin: float = _BOUNDARY_MARGIN) -> bool:
    """VLM 断帧是否其实命中真实镜头边界 → 属正常换镜,不计异常。

    三关全过才返回 True:
      1. t 非空且落在某镜头边界的**单侧豁免窗**内。轮40:每个边界按左右
         两侧分别定尺——eff_side = min(margin, 1.6s 采样覆盖保底,
         该侧相邻镜长)。旧代码对每个边界只算一个"到最近邻边界的距离",
         末镜起点边界拿左邻镜长定尺:短末镜+长前镜时整个短末镜被
         2.0s 窗吞掉(六审 #2 实锤)。Σ 入 bounds 后末镜右侧也有锚点;
      2. 显式 kind='boundary' → 直接豁免;
      3. kind 缺失/标 intra 时:desc 必须含换镜语义词且不含告警词。

    margin 默认 _BOUNDARY_MARGIN(全片终审);单镜诊断(轮12)传更紧的
    边缘窗口——短 clip 上用 2.0s 会把大半个镜头都豁免掉。
    """
    if t_b is None or not bounds:
        return False
    for idx, b in enumerate(bounds):
        # 两端的外侧距离为 0:片头之前/片尾(Σ)之后没有镜,谈不上"采样
        # 偏移"——Σ 的右侧窗口必须是 0,否则尾段帧会被 1.6s 右窗吞掉
        # (六审 #2:尾帧 kind=boundary 被静默豁免的残留路径)。
        d_left = (b - bounds[idx - 1]) if idx > 0 else 0.0
        d_right = (bounds[idx + 1] - b) if idx + 1 < len(bounds) else 0.0
        eff_l = min(margin, _BOUNDARY_MIN_COVER, d_left)
        eff_r = min(margin, _BOUNDARY_MIN_COVER, d_right)
        if not (b - eff_l <= t_b <= b + eff_r):
            continue
        if str(kind).strip() == "boundary":
            return True
        if not any(w in desc for w in _BOUNDARY_SWITCH_WORDS):
            continue
        return not any(w in desc for w in _BOUNDARY_ALARM_WORDS)
    return False


def _context_frames(video_dur: float, shots: list[dict], frames_count: int) -> list[tuple[float, int]]:
    """Per-shot *coverage* sampling — every shot gets open/middle/close frames.

    历史教训（AGENT_REVIEW vs 实片）:旧的「每镜首+尾各1帧、预算 ≤16」采样把
    每个镜头中间约 60% 的画面整段漏掉,且镜头一多整镜裁掉——VLM 审片变成了
    "抽查几帧",与『逐镜全覆盖』的要求相悖。继任两轮迭代后(2026-09-21 审计
    报告 verified):
    - 每镜最少 3 帧(开/中/合),覆盖标准 TVC 镜 2-8s 的全部中段;
    - 帧避开镜头边界 0.45s 的叠化带,避免 dissolve 被 VLM 误判「重复画面」;
    - 预算受 frames_count 钳制;超预算丢帧时优先丢「紧贴镜头边界的帧」
      (叙事信息量最低),保住中段帧,每镜至少保留 budget_len(shots) 帧。
    返回 [(t, shot_index), ...] 时间点+所属镜头。
    """
    per: list[tuple[float, int]] = []
    t = 0.0
    margin = 0.45
    for i, s in enumerate(shots):
        d = float(s.get("duration_sec") or 0)
        if d <= 0:
            d = 0.6
        # 帧数随镜长自适应:约 1 帧/秒(3~5 帧/镜),总帧数上限压在 MAX_REVIEW_FRAMES
        n = max(3, min(FRAMES_PER_SHOT_MAX, int(round(d))))
        span = max(d - 2 * margin, 0.2)
        for j in range(n):
            tt = margin + span * (j + 0.5) / n
            # 不越界、落在视频时长内
            if 0 <= tt <= d:
                per.append((round(t + tt, 2), i))
        t += d

    # 超预算:从「紧贴边界的帧」开始丢(保住信息量最高的中段),每镜至少留 floor 帧
    budget = max(8, min(frames_count, MAX_REVIEW_FRAMES))
    if len(per) <= budget:
        return per
    per.sort(key=lambda x: x[0])
    # 每镜保留下限:预算充裕时 3 帧,预算吃紧时退到 2 帧(绝不到 0)
    floor = max(2, min(3, budget // max(len(shots), 1)))
    keep_min = {i: floor for i in range(len(shots))}
    while len(per) > budget:
        # 优先丢弃「离镜头边界最近」的帧——丢的是开场/收尾过渡帧,
        # 中段最容易崩坏(变形/多指)的帧一定留下
        candidates = [k for k in range(len(per))
                      if _drop_ok(per, k, keep_min)]
        if not candidates:
            break
        per.pop(min(candidates, key=lambda k: _boundary_dist(per[k], shots)))
    return per


def _drop_ok(per, k, keep_min) -> bool:
    """第 k 帧可丢条件:该镜头剩余帧数 ≥ 该镜头最少保留数。"""
    _, idx = per[k]
    cnt = sum(1 for _, i in per if i == idx)
    return cnt > keep_min.get(idx, 2)


def _boundary_dist(frame: tuple[float, int], shots) -> float:
    """帧到所属镜头最近边界的距离(秒)——越小越靠近叠化带,信息量越低。"""
    t, idx = frame
    start = 0.0
    for i, s in enumerate(shots):
        d = float(s.get("duration_sec") or 0)
        if i == idx:
            return min(t - start, start + d - t)
        start += d
    return 0.0


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
    # 轮33:被审视频的内容哈希——终审凭证必须绑定"审的是这条视频"。
    # 缺了它,finalize 无法在发布时刻核对「盘上的 final.mp4 就是当年过的
    # 那条」:assemble 后把成片换掉再 finalize,所有内容门被整体绕过
    # (三审审计的共同根因)。
    try:
        import hashlib as _hl_v
        _video_sha = _hl_v.sha256(video.read_bytes()).hexdigest()
    except OSError:
        _video_sha = ""

    # ── layer 1: deterministic structure pass ──────────────────────
    # 轮37:每个确定性检查独立 try——旧代码五大检查(硬切/音频/运动/
    # 瞬变/黑帧)包在同一个 try 里,任一处探测异常(ffprobe 字段 "N/A"
    # 转 float、黑帧解析炸…)会把**全部**确定性检查降级成一条
    # DETERMINISTIC_PASS_ERROR suggestion:单点失败=整体放弃确定性防线
    # (五审补充项)。现在单点失败只废那一个检查。
    from .clip_qc import (detect_cut_times, _duration_and_dims,
                          _ffprobe, black_spans, transient_spikes)
    from .clip_qc import motion_energy as _motion_energy
    deterministic: dict = {"cut_times": [], "internal_cuts": [],
                           "avg_shot_sec": None}
    det_findings: list[dict] = []
    video_dur = 0.0

    def _det_guard(name: str, fn) -> None:
        try:
            fn()
        except Exception as e:
            det_findings.append({
                "severity": "suggestion", "code": "DETERMINISTIC_PASS_ERROR",
                "message": f"确定性检查[{name}]失败: {type(e).__name__}: "
                           f"{e}"[:200]})

    def _check_cuts() -> None:
        nonlocal video_dur
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

    def _check_audio_motion() -> None:
        # M6(2026-09-21 审计):成片层物理完整性扇区——音频流存在性 + 运动能量。
        # 静态帧 VLM 看不到运动/冻结/J 帧,这是确定性层唯一能补的两块廉价检查。
        _streams = _ffprobe(video).get("streams", [])
        audio_ok = any(s.get("codec_type") == "audio"
                       and float(s.get("duration", 0) or 0) > 0.3
                       for s in _streams)
        deterministic["audio_ok"] = audio_ok
        if not audio_ok:
            det_findings.append({
                "severity": "warning", "code": "FINAL_NO_AUDIO",
                "message": "成片无有效音轨（旁白/氛围声缺失）——音画审查不可用"})
        me = _motion_energy(video)
        deterministic["motion_energy"] = me
        if me < 1.0:
            det_findings.append({
                "severity": "critical", "code": "FINAL_FROZEN",
                "message": f"成片运动能量仅 {me:.2f}(<1.0)——疑似静帧幻灯/冻结画面"})
        elif me < 2.5:
            det_findings.append({
                "severity": "warning", "code": "FINAL_MOTION_LOW",
                "message": f"成片运动能量仅 {me:.2f}(<2.5)——运动不足,注意局部冻结帧"})

    def _check_transient() -> None:
        # 审计 G2:全片亮度瞬变(闪白/闪黑/单帧崩坏)。叠化拼接在镜头边界
        # 前后 ~1s 内会有亮度过渡,属正常;边界之外的瞬变才是病。
        spikes = transient_spikes(video)
        if shots:
            bounds = _shot_boundaries(shots)
            spikes = [s for s in spikes
                      if all(abs(s["t"] - b) > 1.5 for b in bounds)]
        deterministic["transient_spikes"] = spikes
        if len(spikes) >= 2 or any(s["delta"] >= 130 for s in spikes):
            det_findings.append({
                "severity": "critical", "code": "FINAL_TRANSIENT_SPIKES",
                "message": (f"成片检测到 {len(spikes)} 处镜头边界外的亮度瞬变闪帧"
                            f"(t={[s['t'] for s in spikes]})——单帧崩坏/闪场,"
                            f"需定位到对应镜头重新生成")})
        elif len(spikes) == 1:
            det_findings.append({
                "severity": "warning", "code": "FINAL_TRANSIENT_SPIKES",
                "message": (f"成片检测到 1 处镜头边界外的亮度瞬变闪帧"
                            f"(t={spikes[0]['t']}s)——建议复核该时刻")})

    def _check_black() -> None:
        # 轮22:全片黑帧(blackdetect)。叠化拼接的边界两侧 ~1s 内允许
        # 压黑过渡;此外的整段黑屏=生成失败/渲染残帧。与 per-clip 的
        # BLACK_FRAMES 同源不同层:clip 层管素材,这里管成片。
        _bspans = black_spans(video, min_dur=0.5)
        deterministic["black_spans"] = _bspans
        _bnds = _shot_boundaries(shots) if shots else []
        _bad_black = [sp for sp in _bspans
                      if sp[0] > 0.3 and sp[1] < video_dur - 0.3
                      and all(abs(sp[0] - b) > 1.5 and abs(sp[1] - b) > 1.5
                              for b in _bnds)]
        if _bad_black:
            det_findings.append({
                "severity": "critical", "code": "FINAL_BLACK_FRAMES",
                "message": (f"成片检测到 {len(_bad_black)} 处镜内整段黑屏"
                            f"(t={[(round(s, 2), round(e, 2)) for s, e, _ in _bad_black]})"
                            f"——生成失败/渲染残帧,需定位重生成")})

    _det_guard("cuts_pacing", _check_cuts)
    _det_guard("audio_motion", _check_audio_motion)
    _det_guard("transient", _check_transient)
    _det_guard("black", _check_black)

    # ── layer 2: VLM walk-through (per-shot coverage sampling) ─────
    try:
        if shots:
            # 预算按"每镜至少 FRAMES_PER_SHOT 帧"扩张,不再一戳到底的 16 帧封顶
            budget = max(frames_count, len(shots) * FRAMES_PER_SHOT)
            times = _context_frames(video_dur, shots, budget)
        else:
            times = None
    except Exception:
        times = None
    # 轮38:尾部覆盖——final.mp4 长于 Σ分镜时长时(拼接余量/音频床溢出),
    # 旧代码尾段一帧不采也无覆盖断言:未审内容直接进发布物(五审 #3 的
    # 尾部子项)。补采尾段(shot_idx=None → tag「尾部」,批次提示词的叙事
    # 连续检查照看;分镜预期段本就不为无 shot_idx 的帧出行)。
    if times and shots:
        try:
            _shots_total = sum(float(s.get("duration_sec") or 0)
                               for s in shots if isinstance(s, dict))
            _tail = video_dur - _shots_total
            if _tail > 0.5:
                _n_tail = min(4, max(1, int(_tail)))
                for _k in range(_n_tail):
                    times.append((round(_shots_total
                                        + _tail * (_k + 0.5) / _n_tail, 2),
                                  None))
        except Exception:
            pass
    dropped_frames: list[dict] = []
    _tmp_dirs: list[str] = []  # 轮19:批次编码完成后统一删除
    try:
        if times:
            frames = []
            tmp = tempfile.mkdtemp(prefix="vlm_gate_")
            _tmp_dirs.append(str(tmp))
            for i, (t, shot_idx) in enumerate(times):
                p = Path(tmp) / f"f{i:02d}_t{t:06.2f}.png"
                r = subprocess.run(
                    ["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(video),
                     "-frames:v", "1", "-vf", "scale=960:-2", "-y", str(p)],
                    capture_output=True, text=True)
                # 轮40:shot_idx=None = 尾段帧(无分镜归属);固定 tag「尾部」
                # (轮38 的每帧唯一 tag 会让覆盖计数恒 1 → THIN 必触)
                tag = ("尾部" if shot_idx is None
                       else f"镜头{shots[shot_idx].get('shot_id', shot_idx + 1)}")
                if p.exists() and p.stat().st_size > 1000:
                    frames.append({"t": round(t, 2), "path": str(p),
                                   "shot": tag, "shot_idx": shot_idx})
                else:
                    # M3: 抽帧失败不静默——记入 dropped_frames,覆盖断言可见
                    dropped_frames.append({"t": round(t, 2), "shot": tag,
                                           "shot_idx": shot_idx})
        else:
            frames, _ef_tmp = _extract_frames(
                video, max(4, min(frames_count, 16)))
            _tmp_dirs.append(str(_ef_tmp))
    except Exception as e:
        _cleanup_tmp(*_tmp_dirs)
        return {"verdict": "error", "reason": f"抽帧失败: {e}", "findings": []}
    if not frames:
        _cleanup_tmp(*_tmp_dirs)
        return {"verdict": "blocked", "reason": "未能从视频抽取任何帧", "findings": []}

    # ── M3: per-shot 覆盖可断言 ─────────────────────────────────────
    # 每个镜头都有计划帧;实际进入 VLM 的帧数按镜头统计,0 帧镜头必须暴露。
    shot_coverage: dict[str, int] = {}
    coverage_gaps: list[dict] = []
    _planned: dict[str, int] = {}  # 轮40:每 tag 的计划帧数(THIN 按计划判)
    if times:
        for _, shot_idx in times:
            # 轮40:尾段帧用固定 tag「尾部」——轮38 曾用「尾部+X.XXs」每帧
            # 唯一 → 每条 cnt 恒 1 → COVERAGE_THIN 必触 → 凡有尾段的成片
            # 终审恒 fix 无法交付(六审实证)。固定 tag + 按计划帧数判定。
            tag = ("尾部" if shot_idx is None
                   else f"镜头{shots[shot_idx].get('shot_id', shot_idx + 1)}")
            _planned[tag] = _planned.get(tag, 0) + 1
            shot_coverage.setdefault(tag, 0)
        for f in frames:
            if "shot" in f:
                shot_coverage[f["shot"]] = shot_coverage.get(f["shot"], 0) + 1
        for tag, planned_n in sorted(_planned.items()):
            cnt = shot_coverage.get(tag, 0)
            if cnt == 0:
                coverage_gaps.append({"shot": tag, "frames": cnt})
                det_findings.append({
                    "severity": "critical", "code": "COVERAGE_GAP",
                    "message": f"{tag} 没有任何帧进入 VLM 审查——该区域未审,禁止放行"})
            elif cnt < planned_n:
                coverage_gaps.append({"shot": tag, "frames": cnt})
                det_findings.append({
                    "severity": "warning", "code": "COVERAGE_THIN",
                    "message": (f"{tag} 仅 {cnt}/{planned_n} 帧进入 VLM 审查"
                                f"(抽帧失败)——未审部分按未见处理")})
        if dropped_frames:
            det_findings.append({
                "severity": "warning", "code": "FRAMES_DROPPED",
                "message": (f"{len(dropped_frames)} 个采样点抽帧失败(ffmpeg 无输出或过小)"
                            f"——未被审查,见 dropped_frames 字段")})

    batch_results: list[dict] = []
    breaks: list[str] = []
    boundary_transitions: list[dict] = []
    # 剪辑豁免(_is_boundary_transition):kind='boundary' 或 desc 含换镜语义词
    # 的断帧,只有落在真实镜头边界 ±_BOUNDARY_MARGIN 内才豁免(coffee-v7 实跑:
    # 正当转场被误报成 critical 的 5 条即此类);t 远离所有边界或 desc 含告警词
    # (错位/疑似/异常…) → 仍按 critical 处理。
    # 轮40:with_total=True——豁免窗需要 Σ 锚点(末镜右侧按末镜时长定尺、
    # 尾段帧不被右窗吞);确定性滤波仍用无 Σ 的起点集(Σ 非画面真实剪辑点)
    bounds = _shot_boundaries(shots, with_total=True) if shots else []
    brand_seen = False
    anomalies: list[dict] = []
    shot_issues: list[dict] = []
    # M4(2026-09-21 审计):批次间重叠 1 帧的滑动窗口——相邻批次共享一帧,
    # 保证镜头边界两侧的帧对(镜尾 vs 邻镜镜首)必然同批可见,VLM 能审"镜间连接"
    # M8(实测):上游端点硬限制 4 图/请求(>4 直接 400 Image count exceeds limit),
    # 滑动窗必须钳在 4 帧以内,否则真实终片审查必然失败(测试桩看不出)。
    for i in range(0, len(frames), 3):
        batch = (frames[0:4] if i == 0
                 else frames[max(0, i - 1): i + 4][:4])
        times_str = ", ".join(f"{f['t']}" for f in batch)
        # 轮40:请求异常(3 次重试后仍失败:端点宕机/超时)必须走与单镜
        # 路径一致的 critical 处理器——旧代码此处无 try,_ask_vlm 的
        # RuntimeError 直接逃出终审变 500/任务失败,"连不上"与"返回垃圾"
        # 给出两种默认值(垃圾=拦截,宕机=崩溃),而身份通道对同一故障还
        # 静默 skip(六审 #3)。
        try:
            resp = _ask_vlm(_frames_payload(batch),
                            _batch_prompt(times_str, len(batch), ctx, batch),
                            key)
            batch_results.append({"t_range": f"{batch[0]['t']}~{batch[-1]['t']}",
                                  "vlm": resp})
        except Exception as e:
            batch_results.append({"batch": i // 3,
                                  "error": f"{type(e).__name__}: {e}"[:160]})
            det_findings.append({
                "severity": "critical", "code": "VLM_PROTOCOL_VIOLATION",
                "message": (f"终审 VLM 第 {i // 3 + 1} 批"
                            f"(t={batch[0]['t']}~{batch[-1]['t']}s)请求失败"
                            f"({type(e).__name__})——该批帧未审,按未审拦截")})
            continue
        try:
            m = re.search(r"\{.*\}", resp, re.S)
            parsed = json.loads(m.group(0)) if m else {}
        except (json.JSONDecodeError, AttributeError):
            parsed = {}
        # 轮35:协议违约 fail-closed——返回不可解析为 JSON(注入把模型带偏/
        # 模型不守「返回严格 JSON」)时,旧代码 parsed={} 静默放行:该批零
        # findings 零报错,分镜文本可经此单方面关闭内容门(五审 #2)。
        # 与 COVERAGE_GAP 同规格:没审到 = critical,不许读作"审过了"。
        if not parsed:
            det_findings.append({
                "severity": "critical", "code": "VLM_PROTOCOL_VIOLATION",
                "message": (f"终审 VLM 第 {i // 3 + 1} 批"
                            f"(t={batch[0]['t']}~{batch[-1]['t']}s)返回不可"
                            f"解析为 JSON——该批帧未审,按未审拦截")})
            continue
        for b in (parsed.get("breaks") or []):
            if isinstance(b, str):
                if b not in breaks:
                    breaks.append(b)
                continue
            if not isinstance(b, dict):
                continue
            try:
                t_b = float(b.get("t"))
            except (TypeError, ValueError):
                t_b = None
            kind = str(b.get("kind") or "intra")
            desc = str(b.get("desc") or "")[:130]
            if _is_boundary_transition(t_b, kind, desc, bounds):
                # 变量名 bkey:绝不能再覆写外层 key(AGNES 凭据)——2026-09-21
                # 实测事故:此分支把 key 改成 f"{t}|{desc}",后续批次 Authorization
                # 头变成『Bearer 11.09|画面从咖啡店门口…』,latin-1 编码直接崩。
                bkey = f"{t_b:.2f}|{desc}"
                if not any(x["t"] == t_b and x["desc"] == desc
                           for x in boundary_transitions):
                    boundary_transitions.append({"t": t_b, "desc": desc})
                continue
            msg = (f"镜头内画面突变 t={t_b}s：{desc}" if t_b is not None
                   else f"镜头内画面突变：{desc}")
            if msg not in breaks:
                breaks.append(msg)
        if parsed.get("brand_seen"):
            brand_seen = True
        for fr in (parsed.get("frames") or []):
            if isinstance(fr, dict) and fr.get("anomaly"):
                anomalies.append({"t": fr.get("t"), "note": str(fr.get("scene") or "")[:80]})
        for si in (parsed.get("shot_issues") or []):
            if isinstance(si, dict) and str(si.get("shot") or "").strip():
                shot_issues.append(si)

    # 轮19:批次已全部编码发送,抽帧临时目录在此删除(此前从不删,
    # 每个审查调用泄漏一个含 N 张 960px PNG 的目录)
    _cleanup_tmp(*_tmp_dirs)

    # ── 人物一致性(跨镜 + 镜内双通道,VBench-2.0 human_identity 思路) ──
    # 轮11a:守卫从 len(shots)>1 放宽到 shots 非空——单镜视频同样可能
    # 镜内换人(整片一镜到底的广告),此前被直接跳过。
    identity = {"pairs": [], "checked": 0, "findings": [],
                "intra_pairs": [], "intra_checked": 0}
    if shots and key:
        # 轮17:剧本钉了人物外观(anchor/镜主体含服装发型式样)时,跨镜
        # 换装即违反剧本 → COSTUME_SWAP 升 critical
        identity = _identity_gate(video, shots, key,
                                  pinned_look=_look_pinned(
                                      shots, str(ctx.get("actor_anchor")
                                                 or "")))

    all_findings = det_findings + [
        {"severity": "critical", "code": "VLM_BREAK", "message": b} for b in breaks
    ] + [
        {"severity": "critical", "code": "VLM_FRAME_ANOMALY",
         "message": f"t={a['t']}s 帧内容崩坏: {a['note']}"} for a in anomalies[:10]
    ] + [
        {"severity": "critical", "code": "SHOT_STORY_MISMATCH",
         "message": (f"镜头{s['shot']} 画面与分镜剧本不符: {str(s.get('issue') or '')[:120]}"
                     f"——生成的是另一幕却照常放行,必须重新生成该镜")}
        for s in shot_issues[:10]
    ] + identity.get("findings", [])
    # M8(2026-09-21 审计):品牌承诺硬门。brief 声明品牌名时,
    # "品牌全程未入画"=交付级缺陷,必须拦截("XX咖啡大字落版"是广告
    # brief 的硬性交付物;实测 C 变体证明提示词可驱动 brand_seen,
    # 所以未入画=生成失败,不是审查过严)。首/尾镜含品牌提示词的
    # 项目在这里全面收口;无品牌名（纯信息展示）则不受影响。
    if not brand_seen and str(ctx.get("brand_name") or "").strip():
        _bn = ctx.get("brand_name")
        all_findings.append({
            "severity": "critical", "code": "BRAND_MISSING",
            "message": (f"brief 声明的品牌「{_bn}」在成片全程未被 VLM 检测到"
                        f"——品牌未落版，不能作为交付物。请在提示词中明确品牌"
                        f"文字落点(杯身/灯箱/落版卡)后重生成")})
    verdict = "pass" if not all_findings else "fix"
    _reason = (f"确定性 {len(det_findings)} + VLM 断帧 {len(breaks)}"
               f" + 内容崩坏 {len([a for a in anomalies if a])}")
    if boundary_transitions:
        _reason += f"(正当换镜 {len(boundary_transitions)} 处不计)"
    if not brand_seen and str(ctx.get("brand_name") or "").strip():
        _reason += " + 品牌未入画"
    return {
        "verdict": verdict,
        "reason": ("终验通过" if not all_findings else
                   f"终验发现 {len(all_findings)} 处问题（{_reason}）"),
        "brand_seen": brand_seen,
        "breaks": breaks,
        "boundary_transitions": boundary_transitions,
        "anomalies": anomalies,
        "deterministic": deterministic,
        "findings": all_findings,
        "frames_reviewed": len(frames),
        "shot_coverage": shot_coverage,
        "coverage_gaps": coverage_gaps,
        "dropped_frames": dropped_frames,
        "shot_issues": shot_issues,
        "identity": identity,
        "video_sha256": _video_sha,
        "batches": batch_results,
    }


# ── 轮12(2026-09-21):单镜 VLM 符合度诊断 ──────────────────────────────
# 终审是一个 prompt 扛全部分镜预期(30 帧通看),镜头一多预期就被稀释——
# coffee-v7 实测 S05 动作时序、S08 落版判定漂移皆源于此。用户要求
# 「对每个分镜真实最终输出+双重诊断,对照剧情/分镜预期逐帧核验」,
# 确定性侧已有 qc_clip(use_vlm 补 same_scene/morph),VLM 侧缺的正是
# 「逐帧对照分镜文本」的符合度诊断——本函数补上,每镜独立 ctx、
# ≤4 图/请求(端点硬限),外加轮11 的镜内身份通道。
_SHOT_EDGE_MARGIN = 0.75  # 单镜诊断的剪辑边缘豁免窗(见 _is_boundary_transition)


def vlm_review_shot(clip_path: str, shot: dict, frames_count: int = 4,
                    key: Optional[str] = None) -> dict:
    """对单个已生成镜头跑 focused VLM 诊断(单镜 ctx)。

    与 vlm_review_final 的分工:终审管全片(跨镜连接/品牌/时间轴),
    本函数管「这一镜是否真的在演剧本写的那一幕」——
      1) 确定性层:qc_clip(黑帧/内部切镜/首末帧比对,use_vlm=False,
         避免与 pipeline 里已跑的 use_vlm=True 重复问 VLM);
      2) VLM 符合度:_batch_prompt 单镜 ctx(分镜预期逐帧注入),
         4 帧一批(端点 4 图硬限),帧点取 12/38/62/88%;
      3) 镜内身份:_identity_gate(clip, [shot], key)——轮11 通道在单镜
         clip 上自然生效(15%/85% 首末帧)。
    不含品牌门(品牌是全片属性)与跨镜判定(单镜无从比起)。
    断帧边缘豁免用 _SHOT_EDGE_MARGIN:clip 头尾本就是镜界,贴边断帧
    多是裁剪借帧伪影;短 clip 上用全片 2.0s 会豁免掉大半个镜头。
    """
    from .clip_qc import qc_clip, _duration_and_dims
    key = key or _vlm_credentials()
    sid = str(shot.get("shot_id") or "?")
    if not key:
        return {"verdict": "blocked", "shot_id": sid, "findings": [],
                "reason": "AGNES_KEY 未配置；禁止交付"}
    clip = Path(clip_path).resolve()
    if not clip.exists():
        return {"verdict": "blocked", "shot_id": sid, "findings": [],
                "reason": f"clip not found: {clip}"}
    dur = float(shot.get("duration_sec") or 0)
    findings: list[dict] = []
    # 1) 确定性层
    det: dict = {}
    try:
        qc = qc_clip(str(clip), shot_id=sid, expected_duration_sec=dur,
                     use_vlm=False)
        det = {"verdict": qc.get("verdict"), "checks": qc.get("checks")}
        for f in qc.get("findings") or []:
            findings.append(dict(f))
    except Exception as e:
        findings.append({"severity": "suggestion", "code": "SHOT_QC_ERROR",
                         "message": f"镜头{sid} 确定性检查失败: {e}"})
    # 2) VLM 符合度(单镜 ctx,≤4 帧/批)
    frames: list[dict] = []
    dropped: list[float] = []
    try:
        clip_dur, _dims = _duration_and_dims(clip)
    except Exception:
        clip_dur = dur
    if clip_dur <= 0:
        clip_dur = dur
    n = max(2, min(int(frames_count or 4), 4))
    tmp = tempfile.mkdtemp(prefix="vlm_shot_")
    for k in range(n):
        t = round(clip_dur * (0.12 + 0.76 * k / max(1, n - 1)), 2)
        p = Path(tmp) / f"f{k:02d}_t{t:06.2f}.png"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", f"{t}", "-i", str(clip),
             "-frames:v", "1", "-vf", "scale=960:-2", "-y", str(p)],
            capture_output=True, text=True)
        if p.exists() and p.stat().st_size > 1000:
            frames.append({"t": t, "path": str(p), "shot": f"镜头{sid}",
                           "shot_idx": 0})
        else:
            dropped.append(t)
    if not frames:
        _cleanup_tmp(tmp)
        return {"verdict": "blocked", "shot_id": sid, "findings": findings,
                "reason": f"镜头{sid} 未能抽取任何帧", "deterministic": det}
    ctx = {"shots": [dict(shot, duration_sec=clip_dur)],
           "duration_sec": round(clip_dur, 2)}
    bounds = [0.0, round(clip_dur, 2)]
    breaks: list[str] = []
    boundary_transitions: list[dict] = []
    anomalies: list[dict] = []
    shot_issues: list[dict] = []
    batch_results: list[dict] = []
    for i in range(0, len(frames), 3):
        batch = (frames[0:4] if i == 0
                 else frames[max(0, i - 1): i + 4][:4])
        times_str = ", ".join(f"{f['t']}" for f in batch)
        prompt = _batch_prompt(times_str, len(batch), context=ctx, batch=batch)
        try:
            resp = _ask_vlm(
                [{"type": "image_url",
                  "image_url": {"url": "data:image/png;base64," +
                                base64.b64encode(
                                    Path(f["path"]).read_bytes()).decode()}}
                 for f in batch],
                prompt, key, max_tokens=1200)
            m = re.search(r"\{.*\}", resp, re.S)
            parsed = json.loads(m.group(0)) if m else {}
        except Exception as e:
            batch_results.append({"batch": i // 3, "error":
                                  f"{type(e).__name__}: {e}"[:160]})
            # 轮35:请求异常(重试后仍失败)同样是「该批未审」——记 critical
            # 而非静默 continue,没审到不许读作审过了
            findings.append({
                "severity": "critical", "code": "VLM_PROTOCOL_VIOLATION",
                "message": (f"镜头{sid} 单镜诊断第 {i // 3 + 1} 批 VLM 请求"
                            f"失败({type(e).__name__})——该批帧未审,按未审"
                            f"拦截")})
            continue
        # 轮35:协议违约 fail-closed——返回不可解析为 JSON 时旧代码
        # parsed={} 静默放行(分镜文本可经注入单方面关闭内容门)
        if not parsed:
            findings.append({
                "severity": "critical", "code": "VLM_PROTOCOL_VIOLATION",
                "message": (f"镜头{sid} 单镜诊断第 {i // 3 + 1} 批返回不可解析"
                            f"为 JSON——该批帧未审,按未审拦截")})
            continue
        batch_results.append({"batch": i // 3, "t": [f["t"] for f in batch]})
        for b in (parsed.get("breaks") or []):
            if isinstance(b, str):
                if b not in breaks:
                    breaks.append(b)
                continue
            if not isinstance(b, dict):
                continue
            try:
                t_b = float(b.get("t"))
            except (TypeError, ValueError):
                t_b = None
            kind = str(b.get("kind") or "intra")
            desc = str(b.get("desc") or "")[:130]
            if _is_boundary_transition(t_b, kind, desc, bounds,
                                       margin=_SHOT_EDGE_MARGIN):
                if not any(x["t"] == t_b and x["desc"] == desc
                           for x in boundary_transitions):
                    boundary_transitions.append({"t": t_b, "desc": desc})
                continue
            msg = (f"镜头{sid} 单镜诊断 镜头内画面突变 t={t_b}s：{desc}"
                   if t_b is not None
                   else f"镜头{sid} 单镜诊断 镜头内画面突变：{desc}")
            if msg not in breaks:
                breaks.append(msg)
        for fr in (parsed.get("frames") or []):
            if isinstance(fr, dict) and fr.get("anomaly"):
                anomalies.append({"t": fr.get("t"),
                                  "note": str(fr.get("scene") or "")[:80]})
        for si in (parsed.get("shot_issues") or []):
            if isinstance(si, dict) and str(si.get("issue") or "").strip():
                shot_issues.append({"shot": str(si.get("shot") or sid),
                                    "issue": str(si.get("issue"))[:200]})
    # 轮19:批次已全部编码发送,抽帧临时目录在此删除
    _cleanup_tmp(tmp)
    for b in breaks:
        findings.append({"severity": "critical", "code": "VLM_BREAK",
                         "message": b})
    for a in anomalies[:10]:
        findings.append({"severity": "critical", "code": "VLM_FRAME_ANOMALY",
                         "message": f"镜头{sid} t={a['t']}s 帧内容崩坏: "
                                    f"{a['note']}"})
    for si in shot_issues:
        findings.append({"severity": "critical", "code": "SHOT_STORY_MISMATCH",
                         "message": f"镜头{si['shot']} 单镜诊断: "
                                    f"{si['issue']}"})
    # 3) 镜内身份(轮11 通道在单镜 clip 上复用)
    identity: dict = {"pairs": [], "checked": 0, "findings": [],
                      "intra_pairs": [], "intra_checked": 0}
    try:
        identity = _identity_gate(clip, [dict(shot, duration_sec=clip_dur)],
                                  key)
        findings.extend(identity.get("findings") or [])
    except Exception as e:
        identity = {"error": f"{type(e).__name__}: {e}"[:160]}
    verdict = "fix" if any(f.get("severity") == "critical"
                           for f in findings) else "pass"
    return {
        "verdict": verdict,
        "shot_id": sid,
        "reason": (f"镜头{sid} 单镜诊断{'通过' if verdict == 'pass' else '发现 '
                     + str(len([f for f in findings if f.get('severity') == 'critical']))
                     + ' 处 critical'}"),
        "findings": findings,
        "breaks": breaks,
        "boundary_transitions": boundary_transitions,
        "anomalies": anomalies,
        "shot_issues": shot_issues,
        "identity": identity,
        "deterministic": det,
        "frames_reviewed": len(frames),
        "dropped_frames": dropped,
        "batches": batch_results,
    }