"""Subtitle rendering — adapted from openshorts/subtitles.py + hypit caption.

Provides ASS style generation, SRT burning, and word-level karaoke captions
(对齐 hypit caption 的词级卡拉OK字幕：彩色词盒逐词点亮，与音频逐字同步).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional


def hex_to_ass_color(hex_color: str, opacity: float = 1.0) -> str:
    """Convert #RRGGBB to ASS &HAABBGGRR format."""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 6:
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    elif len(hex_color) == 8:
        r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
        opacity = int(hex_color[6:8], 16) / 255.0
    else:
        r, g, b = 255, 255, 255
    alpha = round((1.0 - opacity) * 255)
    return f"&H{alpha:02X}{b:02X}{g:02X}{r:02X}"


def escape_subtitles_path(path: Path) -> str:
    """Escape a path for FFmpeg's subtitles filter.

    Windows drive colon must be escaped: D:/x.srt -> D\\:/x.srt
    (otherwise FFmpeg parses the colon as an option separator).
    """
    s = str(path).replace("\\", "/")
    return s.replace(":", r"\:")


def build_ass_style(
    font_name: str = "Microsoft YaHei",
    font_size: int = 18,
    text_color: str = "#FFFFFF",
    outline_color: str = "#000000",
    outline_width: int = 3,
    shadow: int = 2,
    margin_v: int = 50,
    alignment: int = 2,
    bold: bool = True,
) -> str:
    """Build ASS force_style string for FFmpeg subtitles filter."""
    primary = hex_to_ass_color(text_color)
    outline = hex_to_ass_color(outline_color)
    parts = [
        f"FontName={font_name}",
        f"FontSize={font_size}",
        f"Bold={'-1' if bold else '0'}",
        f"PrimaryColour={primary}",
        f"OutlineColour={outline}",
        "BorderStyle=1",
        f"Outline={outline_width}",
        f"Shadow={shadow}",
        f"MarginV={margin_v}",
        f"Alignment={alignment}",
    ]
    return ",".join(parts)


def generate_srt(segments: list[dict], output_path: Path) -> Path:
    """Generate SRT file from Whisper-style segments.

    Each segment: {"start": float, "end": float, "text": str}
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, seg in enumerate(segments, 1):
        start = _format_ts(seg["start"])
        end = _format_ts(seg["end"])
        text = seg["text"].strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def _format_ts(seconds: float) -> str:
    """Convert seconds to SRT timestamp HH:MM:SS,mmm.

    Rounds to the nearest millisecond and carries the rollover into the
    seconds/minutes (e.g. 59.9997s -> 00:01:00,000, never 00:00:59,000).
    """
    total_ms = round(seconds * 1000)
    hours, rem = divmod(total_ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _format_ass_ts(seconds: float) -> str:
    """Convert seconds to ASS timestamp H:MM:SS.CC (centiseconds)."""
    total_cs = round(seconds * 100)
    hours = total_cs // 360000
    minutes = (total_cs % 360000) // 6000
    cs = total_cs % 6000
    secs, centis = divmod(cs, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def generate_ass_from_srt(srt_path: Path, ass_path: Path, style: str,
                           width: int = 1920, height: int = 1080) -> Path:
    """Convert SRT to ASS with a force_style string (same format as
    build_ass_style output, e.g. "FontName=...,FontSize=...").

    轮53:width/height 写入 PlayResX/PlayResY——libass 的 FontSize 是
    **脚本分辨率**单位,烧到不同尺寸视频时必须让 PlayRes 等于视频尺寸,
    否则字号被缩放(实测 ffmpeg 内部 SRT→ASS 默认画布下 46px 渲染成
    ~86px,短行字幕超宽 1.9×,§10.6 验收每次都拦)。"""
    import re
    srt_content = srt_path.read_text(encoding="utf-8")
    blocks = re.findall(
        r"(\d+)\n([0-9:,.\- ]+) --> ([0-9:,.\- ]+)\n(.+?)(?=\n\n|\Z)",
        srt_content, re.DOTALL)

    # Build a valid [V4+] Styles line from the force_style key=value pairs.
    defaults = {
        "Fontname": "Microsoft YaHei", "Fontsize": "18",
        "PrimaryColour": "&H00FFFFFF", "SecondaryColour": "&H000000FF",
        "OutlineColour": "&H00000000", "BackColour": "&H80000000",
        "Bold": "-1", "Italic": "0", "Underline": "0", "StrikeOut": "0",
        "ScaleX": "100", "ScaleY": "100", "Spacing": "0", "Angle": "0",
        "BorderStyle": "1", "Outline": "3", "Shadow": "2",
        "Alignment": "2", "MarginL": "10", "MarginR": "10",
        "MarginV": "50", "Encoding": "1",
    }
    # force_style keys are case-insensitive in ASS; build_ass_style emits
    # "FontName=/FontSize=" while the defaults table uses "Fontname/Fontsize".
    key_map = {k.lower(): k for k in defaults}
    for pair in (style or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            canonical = key_map.get(k.strip().lower())
            if canonical:
                defaults[canonical] = v.strip()
    style_line = ",".join(defaults[k] for k in (
        "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour",
        "OutlineColour", "BackColour", "Bold", "Italic", "Underline",
        "StrikeOut", "ScaleX", "ScaleY", "Spacing", "Angle", "BorderStyle",
        "Outline", "Shadow", "Alignment", "MarginL", "MarginR", "MarginV",
        "Encoding"))

    ass_header = f"""[Script Info]
Title: Auto-generated from SRT
ScriptType: v4.00+
PlayResX: {int(width)}
PlayResY: {int(height)}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{style_line}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for block in blocks:
        _, start, end, text = block
        clean_text = text.strip().replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
        start_cs = _srt_ts_to_seconds(start.strip())
        end_cs = _srt_ts_to_seconds(end.strip())
        events.append(
            f"Dialogue: 0,{_format_ass_ts(start_cs)},{_format_ass_ts(end_cs)},"
            f"Default,,0,0,0,,{clean_text}")

    ass_content = ass_header + "\n".join(events)
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(ass_content, encoding="utf-8")
    return ass_path


def _srt_ts_to_seconds(ts: str) -> float:
    """Parse SRT (HH:MM:SS,mmm) or ASS (H:MM:SS.cc) timestamp to seconds."""
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    while len(parts) < 3:
        parts.insert(0, "0")
    h, m, s = parts
    return int(h) * 3600 + int(m) * 60 + float(s)


# ── Word-level karaoke captions (hypit caption 词级卡拉OK) ──────────────────
# 输入: whisper word-level anchors (见 whisper_service.extract_word_anchors)
# 输出: ASS dialogue,每个词一个 \k<centi> 标签 → libass 逐词刷色:
#   正在读的词用 SecondaryColour(高亮),已读/未读用 PrimaryColour。

def _ass_escape_karaoke(text: str) -> str:
    """Escape text inside ASS \\k tags (braces are tag syntax, backslash escapes)."""
    return (text.replace("\\", r"\\")
                .replace("{", r"\{")
                .replace("}", r"\}"))


def build_karaoke_ass_header(width: int = 1920, height: int = 1080,
                             style: Optional[dict] = None) -> str:
    """ASS header for one karaoke caption style.

    SecondaryColour is the "lit" color (highlight); PrimaryColour is base.
    """
    defaults = {
        "Fontname": "Microsoft YaHei", "Fontsize": "18",
        "PrimaryColour": "&H00FFFFFF", "SecondaryColour": "&H0000FFFF",
        "OutlineColour": "&H00000000", "BackColour": "&H80000000",
        "Bold": "-1", "Italic": "0", "Underline": "0", "StrikeOut": "0",
        "ScaleX": "100", "ScaleY": "100", "Spacing": "0", "Angle": "0",
        "BorderStyle": "1", "Outline": "3", "Shadow": "2",
        "Alignment": "2", "MarginL": "10", "MarginR": "10",
        "MarginV": "50", "Encoding": "1",
    }
    kara = dict(defaults)
    kara.update(style or {})
    style_line = ",".join(kara[k] for k in (
        "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour",
        "OutlineColour", "BackColour", "Bold", "Italic", "Underline",
        "StrikeOut", "ScaleX", "ScaleY", "Spacing", "Angle", "BorderStyle",
        "Outline", "Shadow", "Alignment", "MarginL", "MarginR", "MarginV",
        "Encoding"))
    return f"""[Script Info]
Title: Karaoke captions
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{style_line}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def generate_karaoke_ass(
    word_groups: list[dict],
    output_path: Path,
    style: Optional[dict] = None,
    width: int = 1920,
    height: int = 1080,
) -> Path:
    """Build a karaoke ASS from word groups.

    word_groups: [{"start": float, "end": float,
                   "words": [{"text","start","end"}, ...]}]
    每个词生成 \\k 时间增量 → 逐词点亮(SecondaryColour 高亮)。
    """
    groups = []
    for group in word_groups:
        words = group.get("words") or []
        if not words:
            g_start = float(group.get("start", 0.0))
            g_end = float(group.get("end", 0.0))
            words = [{"text": group.get("text", ""), "start": g_start, "end": g_end}]
        g_start = float(words[0]["start"]) if words else float(group.get("start", 0.0))
        total = float(group.get("end", words[-1]["end"])) - g_start
        if total <= 0:
            continue
        kara = []
        prev = 0.0
        for i, w in enumerate(words):
            w_start = float(w.get("start", prev))
            w_end = float(w.get("end", w_start + 0.01))
            if i < len(words) - 1:
                seg_cs = max(0, int(round((w_end - w_start) * 100)))
            else:
                # 末词: 吸收剩余时长，避免句尾提前熄灭
                seg_cs = max(0, int(round((total - (w_start - g_start)) * 100)))
            kara.append(f"{{\\k{seg_cs}}}{_ass_escape_karaoke(w.get('text', ''))}")
            prev = w_end
        groups.append((g_start, float(group.get("end", prev)), "".join(kara)))

    header = build_karaoke_ass_header(width=width, height=height, style=style)
    events = [f"Dialogue: 0,{_format_ass_ts(s)},{_format_ass_ts(e)},Karaoke,,0,0,0,,{t}"
              for s, e, t in groups]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return output_path


def _build_karaoke_ass(word_anchors_path, out, karaoke_style, w, h) -> _Path:
    """Load whisper word anchors (word_anchors.json), group by segment, emit ASS.

    word_anchors.json 结构见 whisper_service.extract_word_anchors:
    {"anchors": [{"text","start","end","segment"}, ...]}；
    按 segment 分组 → generate_karaoke_ass → 输出 <out>.karaoke.ass。
    """
    import json as _json
    data = _json.loads(_Path(word_anchors_path).read_text(encoding="utf-8"))
    anchors = data.get("anchors") if isinstance(data, dict) else data
    anchors = anchors or []
    by_seg: dict = {}
    order: list[str] = []
    for a in anchors:
        seg = str(a.get("segment", "s0"))
        if seg not in by_seg:
            by_seg[seg] = {"start": float(a.get("start", 0.0)),
                           "end": float(a.get("end", 0.0)), "words": []}
            order.append(seg)
        by_seg[seg]["words"].append(a)
        by_seg[seg]["end"] = max(by_seg[seg]["end"], float(a.get("end", 0.0)))
    groups = [by_seg[k] for k in order]
    style = dict(karaoke_style or {})
    style.setdefault("Fontsize", str(max(16, int(h * 0.035))))
    style.setdefault("MarginV", "96")
    karaoke_path = _Path(out).with_name(_Path(out).stem + ".karaoke.ass")
    return generate_karaoke_ass(groups, karaoke_path, style=style,
                                width=w, height=h)


# ── Robust subtitle burning ────────────────────────────────────────────────
#
# 实测（2026-09）：本机 FFmpeg 的 subtitles 滤镜（libass）对 SRT/ASS 全部静默
# 不渲染字形，旧 burn 端点因此"成功但无字幕"。改后的链路：
#   probe_libass()            首次调用做一次廉价探测并缓存
#   render_subtitles_best()   按 mode 选路；drawtext 按实战验证过的逐行公式烧录
#   verify_subtitle_cues()    逐 cue 对"原片帧 vs 成片帧"做像素差度量 → 客观数据
# 本文件只输出读数与实现说明，不下通过/不通过结论（判断由监督者做）。

import re as _re
import subprocess as _subprocess
import tempfile as _tempfile
from pathlib import Path as _Path

_LIBASS_PROBE: dict | None = None


def parse_srt_cues(srt_path) -> list[dict]:
    """Parse SRT into [{start, end, text, lines}]. Splits on blank lines, so
    multi-line cue bodies survive (a block-regex with lookahead dropped them)."""
    content = _Path(srt_path).read_text(encoding="utf-8-sig")
    cues = []
    for block in content.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        if len(lines) < 3:
            continue
        m = _re.match(r"^\d{1,2}:\d{2}:\d{2}[,.]\d+\s*-->\s*\d{1,2}:\d{2}:\d{2}[,.]\d+$", lines[1])
        if not m:
            continue
        t0, t1 = lines[1].split("-->")
        start = _srt_ts_to_seconds(t0.strip())
        end = _srt_ts_to_seconds(t1.strip())
        text = "\n".join(lines[2:])
        cues.append({
            "start": start, "end": end, "text": text,
            "lines": [ln for ln in text.splitlines() if ln.strip()],
        })
    if not cues:
        raise ValueError(f"no cues found in {srt_path}")
    return cues


def _band_has_white_text(png_path: _Path, margin_b: int = 8) -> bool:
    """Any bright pixel band? Only >= 200 px counts as real glyph ink —
    22 stray pixels (edge noise) previously slipped through and flipped the
    probe to "libass OK", which the post-render verify then disproved."""
    try:
        from PIL import Image
    except Exception:
        return False
    img = Image.open(png_path).convert("RGB")
    w, h = img.size
    px = img.load()
    bright = 0
    for y in range(max(0, h - margin_b - 3), h):
        for x in range(w):
            r, g, b = px[x, y]
            if r + g + b > 150:
                bright += 1
                if bright >= 200:
                    return True
    return False


def probe_libass() -> dict:
    """Cheap one-frame probe: does subtitles= actually paint glyphs here?"""
    global _LIBASS_PROBE
    if _LIBASS_PROBE is not None:
        return dict(_LIBASS_PROBE)
    try:
        tmp = _Path(_tempfile.gettempdir())
        srt = tmp / "_zcode_libass_probe.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:00,400\n测试 Test\n", encoding="utf-8")
        png = tmp / "_zcode_libass_probe.png"
        style = build_ass_style(font_name="Microsoft YaHei", font_size=12, margin_v=4)
        r = _subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=black:s=320x90:d=0.6",
             "-vf", f"subtitles='{escape_subtitles_path(srt)}':force_style='{style}'",
             "-frames:v", "1", str(png)],
            capture_output=True, text=True, shell=False)
        ok = False
        detail = f"ffmpeg rc={r.returncode}"
        if r.returncode == 0 and png.exists():
            ok = _band_has_white_text(png)
            detail = "glyphs-found" if ok else "glyphs-absent"
        _LIBASS_PROBE = {"libass": ok, "detail": detail}
    except Exception as e:  # probe failure ⇒ assume broken, drawtext path
        _LIBASS_PROBE = {"libass": False, "detail": f"probe-error: {e}"}
    return dict(_LIBASS_PROBE)


def _resolve_font_path(font_path=None) -> str:
    """Pick a drawtext-usable CJK font. Prefer colon-free cwd-relative paths:
    a drive colon splits the filtergraph option, so absolute C:\\ paths must
    be avoided at rest. Falls back to copying a Windows font into _tools/fonts."""
    if font_path:
        p = _Path(font_path)
        if not p.exists():
            raise FileNotFoundError(font_path)
        return p.as_posix()
    cands = [_Path("test_out/_tools/fonts/msyh.ttc"),
             _Path("fonts/msyh.ttc"),
             _Path("fonts/msyhbd.ttc")]
    for c in cands:
        if c.exists():
            return c.as_posix()
    import shutil
    for win in (_Path("C:/Windows/Fonts/msyh.ttc"),
                _Path("C:/Windows/Fonts/msyhbd.ttc"),
                _Path("C:/Windows/Fonts/simhei.ttf")):
        if win.exists():
            dst_dir = _Path("test_out/_tools/fonts")
            dst_dir.mkdir(parents=True, exist_ok=True)
            local = dst_dir / win.name
            if not local.exists():
                shutil.copy2(win, local)
            return local.as_posix()
    raise FileNotFoundError("no CJK font found (msyh.ttc / simhei.ttf)")


_FONT_CACHE: dict = {}


def measure_text_px(text: str, font_size: int, font_path=None) -> Optional[int]:
    """轮51:用 drawtext/libass 实际使用的同一字体度量文本像素宽。

    为什么需要它：字幕折行曾按字符数估算（1 字 ≈ 1 em），但 CJK 字形
    advance 实测 ≈1.04em（46px 字号 ≈48px/字）——17 字预算（按
    0.62×1280/46 算）烧出来 816px = 63.7% 屏宽，恰好被 §10.6 验收门
    （≤62%）打死（E2E 第 8 次实证，字幕成为 generate 全过之後唯一
    的卡点）。用真字体度量后折行预算与渲染结果一致。
    PIL/字体不可用 → None（调用方回退字符估算）。
    字体对象与 Draw 单件按 (path, size) 缓存——_wrap_to_width 逐字符
    度量,每次重新 truetype 加载 + ImageDraw 初始化会让 40 字长句的
    折行慢一个数量级(实测 300 句 >120s → <1s)。"""
    if not text:
        return 0
    try:
        from PIL import Image, ImageDraw, ImageFont
        fp = _resolve_font_path(font_path)
        key = (fp, int(font_size))
        ent = _FONT_CACHE.get(key)
        if ent is None:
            f = ImageFont.truetype(fp, int(font_size))
            d = ImageDraw.Draw(Image.new("RGB", (8, 8)))
            ent = (f, d)
            _FONT_CACHE[key] = ent
        f, d = ent
        return int(d.textlength(str(text), font=f))
    except Exception:
        return None


def _ffprobe_size(video: _Path) -> tuple[int, int]:
    r = _subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        raise RuntimeError("ffprobe failed: " + (r.stderr or "")[-300:])
    w, h = r.stdout.strip().split(",")[:2]
    return int(w), int(h)


def _build_drawtext_chain(cues, font: str, font_size: int, margin_v: int,
                          w: int, h: int, cue_dir: _Path) -> tuple[str, int]:
    """Per-line drawtext nodes using the layout verified in gen_sub_filter.py:

        y = H - margin_v - (行数 - 行idx) * (font_size * 1.2)
    every line gets its own textfile (no colons/commas in the path); cue
    windows are escaped commas, e.g. enable=between(t\\,3.0\\,7.0)."""
    line_h = int(font_size * 1.2)
    nodes = []
    cwd = _Path.cwd()
    for i, cue in enumerate(cues, 1):
        lines = cue["lines"]
        n = len(lines)
        for j, ln in enumerate(lines):
            tf = cue_dir / f"c{i}_{j}.txt"
            tf.write_text(ln, encoding="utf-8")
            # textfile= must never carry a drive colon or a comma into the
            # filtergraph (both split the option); prefer a cwd-relative path.
            try:
                rel = tf.resolve().relative_to(cwd.resolve()).as_posix()
            except ValueError:
                rel = tf.as_posix().replace(":", "\\:").replace(",", "\\,")
            y = h - margin_v - (n - j) * line_h
            nodes.append(
                f"drawtext=fontfile={font}:fontsize={font_size}:fontcolor=white:"
                f"borderw=4:bordercolor=black:shadowx=2:shadowy=2:"
                f"x=(w-text_w)/2:y={y}:"
                f"textfile={rel}:"
                f"enable=between(t\\,{cue['start']:.3f}\\,{cue['end']:.3f})")
    return ",".join(nodes), len(nodes)


def _video_duration(path: _Path) -> Optional[float]:
    """轮57:视频时长探测(字幕 cue 越界判定用);失败返回 None。"""
    try:
        r = _subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, shell=False)
        return float((r.stdout or "").strip())
    except (ValueError, OSError):
        return None


def verify_subtitle_cues(baseline_video: _Path, burned_video: _Path,
                         cues: list[dict], font_size: int = 46,
                         margin_v: int = 96, w: int = 1920, h: int = 1080) -> list[dict]:
    """Per-cue objective measurement: diff the burned frame vs the baseline
    frame at the cue midpoint (bottom band only), so caption ink = changed px.

    Report fields are measurements only — the calling agent decides verdicts."""
    try:
        from PIL import Image
    except Exception as e:
        return [{"index": i + 1, "measured": False, "error": str(e)}
                for i in range(len(cues))]
    line_h = int(font_size * 1.2)
    band_top = max(0, h - margin_v - line_h * 3)
    thr = 240  # *summed* RGB delta per pixel counts as "ink"
    # 轮57(真实使用发现,子智能体 C):cue 超出视频时长时,中点 -ss 越
    # EOF → ffmpeg 不产帧 → Image.open 裸抛 FileNotFoundError,冒到
    # 端点是 404 + 内部 tmp 路径(调用方无法定位「字幕比片长」)。这里
    # 先探片长,越界 cue 直接标记 beyond_duration + measured:False,
    # 由 check_subtitle_cues 转 critical violation。
    _dur = _video_duration(_Path(baseline_video))
    results = []
    for i, cue in enumerate(cues, 1):
        t = (cue["start"] + cue["end"]) / 2
        stats = {"index": i, "start": cue["start"], "end": cue["end"],
                 "measured": True, "found": False, "changed_px": 0,
                 "width_pct": 0.0, "height_px": 0, "rows": 0, "y_range": None}
        if _dur is not None and (cue["end"] > _dur + 0.05 or t > _dur):
            stats.update(measured=False, beyond_duration=True,
                         video_duration=round(_dur, 2))
            results.append(stats)
            continue
        with _tempfile.TemporaryDirectory() as td:
            td = _Path(td)
            bg_p, fg_p = td / "bg.png", td / "fg.png"
            for src, outp in ((baseline_video, bg_p), (burned_video, fg_p)):
                _subprocess.run(
                    ["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", str(src),
                     "-frames:v", "1", str(outp)],
                    capture_output=True, text=True, shell=False)
            try:
                bg = Image.open(bg_p).convert("RGB")
                fg = Image.open(fg_p).convert("RGB")
            except (FileNotFoundError, OSError) as e:
                # 轮57:帧抽取失败(时点越界/文件损坏)标记 measured:False
                # + 可读原因——旧行为裸抛 FileNotFoundError(内部 tmp 路径
                # 泄露出端点是 404,调用方无法定位)
                stats.update(measured=False,
                             error=f"帧抽取失败: {type(e).__name__}")
                results.append(stats)
                continue
            bp, fp = bg.load(), fg.load()
            ww, hh = bg.size
            xmin, xmax, ymin, ymax = ww, -1, hh, -1
            count = 0
            rows = {}
            for y in range(band_top, hh):
                for x in range(ww):
                    br, bgc, bb = bp[x, y]
                    fr, fgc, fb = fp[x, y]
                    if (abs(int(fr) - int(br)) + abs(int(fgc) - int(bgc)) +
                            abs(int(fb) - int(bb))) > thr:
                        count += 1
                        rows[y] = rows.get(y, 0) + 1
                        xmin, xmax = min(xmin, x), max(xmax, x)
                        ymin, ymax = min(ymin, y), max(ymax, y)
            if count:
                stats.update(found=True, changed_px=count,
                             width_pct=round((xmax - xmin + 1) / ww * 100, 1),
                             height_px=ymax - ymin + 1,
                             y_range=[int(ymin), int(ymax)])
                # cluster active rows into visual lines (gap > line_h*0.6)
                active = sorted(rows)
                groups = 1
                for a, b in zip(active, active[1:]):
                    if b - a >= int(line_h * 0.6):
                        groups += 1
                stats["rows"] = groups
        results.append(stats)
    return results


def render_subtitles_best(video_path, srt_path, output_path,
                          font_size: int = 46, margin_v: int = 96,
                          font_path=None, mode: str = "auto",
                          verify: bool = True,
                          word_anchors_path=None,
                          karaoke_style=None) -> dict:
    """Burn SRT into video, picking a strategy that actually paints glyphs.

    mode:
      subtitle      probe libass capability once, pick glyph-painting path
      drawtext      force per-line drawtext (the verified fallback)
      karaoke       word-level karaoke captions: needs whisper word anchors
                    (word_anchors_path, see whisper_service.extract_word_anchors)
                    and libass; falls back to plain drawtext if libass is absent
    Returns measurements in `cues`; verdicts belong to the caller."""
    video, srt, out = _Path(video_path), _Path(srt_path), _Path(output_path)
    if mode == "karaoke" and not word_anchors_path:
        raise ValueError("karaoke mode requires word_anchors_path "
                         "(whisper word-level anchors JSON)")
    for p, name in ((video, "video"), (srt, "srt")):
        if not p.exists():
            raise FileNotFoundError(f"{name} not found: {p}")
    cues = parse_srt_cues(srt)
    font = _resolve_font_path(font_path)
    w, h = _ffprobe_size(video)
    probe = probe_libass()
    strategy = "drawtext"
    karaoke_ass: _Path | None = None
    if mode == "karaoke":
        karaoke_ass = _build_karaoke_ass(word_anchors_path, out,
                                         karaoke_style, w, h)
        # \k 逐词点亮只有 libass 支持；缺失时退化为逐行 drawtext
        # （karaoke=degraded 标记让上层知道亮点丢了）
        strategy = "subtitle" if probe["libass"] else "drawtext"
    elif mode == "subtitle":
        strategy = "subtitle"
    elif mode == "auto" and probe["libass"]:
        strategy = "subtitle"
    out.parent.mkdir(parents=True, exist_ok=True)

    def _run_subtitle_pass():
        # 轮53:SRT 直接喂 ffmpeg 的 subtitles= 时,内部 SRT→ASS 转换的
        # 画布与视频尺寸无关 → FontSize 被放大 ~1.9×(实测 46px 渲染成
        # ~86px),短行字幕超宽、§10.6 验收每次都拦。改为先转成
        # PlayRes=视频尺寸的 ASS 再烧——libass 的 FontSize 即视频像素,
        # 与 drawtext/PIL 度量一致(ratio≈1.0)。
        if karaoke_ass is not None:
            src_path = karaoke_ass  # karaoke 自带样式,不 force_style
            # (\k 逐词点亮的 SecondaryColour 会被 force_style 抹掉)
            vf = f"subtitles='{escape_subtitles_path(src_path)}'"
        else:
            ass_path = out.parent / f"._{out.stem}.ass"
            generate_ass_from_srt(
                srt, ass_path,
                build_ass_style(font_name="Microsoft YaHei",
                                font_size=font_size, margin_v=margin_v),
                width=w, height=h)
            src_path = ass_path
            vf = f"subtitles='{escape_subtitles_path(src_path)}'"
        r = _subprocess.run(
            ["ffmpeg", "-y", "-i", str(video),
             "-vf", vf,
             "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
             str(out)],
            capture_output=True, text=True, shell=False)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-500:])

    def _run_drawtext_pass():
        # cue 文本文件必须落在 cwd 内：textfile= 带盘符的绝对值在部分
        # ffmpeg 构建的 filtergraph 解析下失败（`D\:` 转义不可靠），
        # 放 cwd 下让 _build_drawtext_chain 的 relative_to(cwd) 输出无盘符路径。
        cue_dir = _Path.cwd() / f"._cues_{out.stem}"
        cue_dir.mkdir(parents=True, exist_ok=True)
        chain, n_nodes = _build_drawtext_chain(cues, font, font_size, margin_v,
                                               w, h, cue_dir)
        # filter_complex needs an explicit input binding and a labeled output
        chain = f"[0:v]{chain}[vout]"
        script = out.parent / f"._subfilter_{out.stem}"
        script.write_text(chain, encoding="utf-8")
        r = _subprocess.run(
            ["ffmpeg", "-y", "-i", str(video), "-filter_complex_script",
             str(script), "-map", "[vout]", "-map", "0:a?",
             "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
             str(out)],
            capture_output=True, text=True, shell=False)
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-500:])

    def _measure():
        if not verify:
            return []
        return verify_subtitle_cues(video, out, cues, font_size=font_size,
                                    margin_v=margin_v, w=w, h=h)

    # pass 1: chosen strategy
    if strategy == "subtitle":
        _run_subtitle_pass()
    else:
        _run_drawtext_pass()
    stats = _measure()
    ink = sum(1 for c in stats if c.get("found"))
    # pass 2: if the strategy produced zero painted glyphs, fall back to the
    # other implementation instead of shipping a silent no-subtitle video
    if ink == 0 and strategy == "subtitle" and mode != "subtitle":
        strategy = "drawtext"
        _run_drawtext_pass()
        stats = _measure()
        ink = sum(1 for c in stats if c.get("found"))
    if ink == 0:
        raise RuntimeError(
            "subtitle render painted no visible glyphs (subtitle + drawtext "
            "both produced zero ink) — refusing to return a false pass")
    result = {"ok": True, "output": str(out), "strategy": strategy,
              "font": font, "libass_probe": probe_libass(), "cues": stats}
    if mode == "karaoke":
        result["karaoke_ass"] = str(karaoke_ass)
        # 有 \k 点亮才算真 karaoke；退化走 drawtext 时如实上报
        result["karaoke_degraded"] = karaoke_ass is None or strategy != "subtitle"
    return result


# ── 轮27:字幕验收硬门(§10.6)共享实现 ────────────────────────────────
# 此前只在 api.py 的手工 /burn 端点内联,主链路 assemble 烧完字幕直接
# 放行(out["burn"]["violations"] 恒为 None)——无墨迹/超宽/越安全区的
# 不可读字幕直达终审。提取为共享函数,两端同用。
# 严重级划分(轮27 实测后定):found=false 与 width>红线 是明确的不可读
# 缺陷 → critical;y 带(下缘与屏底距离)记 warning——AGENT_GUIDE 的
# 「下缘≥屏高−110px」与 margin_v=96 的默认排版互相矛盾(实测底缘落在
# ~h-116,按现规则每个项目都会"违规"),属排版参数问题,不硬拦。


def check_subtitle_cues(cues: list, video_path: str,
                        max_width_pct: float = 62.0,
                        margin_allowance: int = 110) -> list[dict]:
    """字幕验收:返回 violations 列表(空=通过)。never raises。

    每项 {"cue": idx, "severity": "critical"|"warning", "issues": [...]}:
      - beyond_duration=true    → critical(cue 结束于片长之后,不显示;
                                  轮57,真实使用发现)
      - measured=false          → warning(帧抽取失败等,不视为通过但不
                                  误伤;轮57)
      - found=false            → critical(无墨迹,字幕没烧上)
      - width_pct > max_width  → critical(超宽出屏/被裁)
      - y 带越界              → warning(排版安全区,见上方说明)
    """
    violations: list[dict] = []
    if not cues:
        return violations
    try:
        w, h = _ffprobe_size(_Path(video_path))
    except Exception:
        w, h = 0, 0
    _video_h = h or 1080
    _y_threshold = _video_h - margin_allowance
    for cue in cues:
        if not isinstance(cue, dict):
            continue
        crit, warn = [], []
        # 轮57(真实使用发现,子智能体 C):cue 超出视频时长——该字幕永远
        # 不会显示(手工 burn 链的常见输入:字幕比成片长)。critical,
        # 文案带 cue 序号/结束秒/片长,不再让调用方猜。
        if cue.get("beyond_duration"):
            crit.append(f"字幕 cue #{cue.get('index')} 结束于 "
                        f"{cue.get('end')}s,超出视频时长 "
                        f"{cue.get('video_duration')}s——该字幕不会显示")
        if cue.get("measured") is False and not cue.get("beyond_duration"):
            warn.append(f"cue #{cue.get('index')} 未能测量"
                        f"({cue.get('error', 'unknown')}),不视为通过")
        if not cue.get("found") and not cue.get("beyond_duration"):
            crit.append("未检测到墨迹(found=false)")
        if float(cue.get("width_pct", 0) or 0) > max_width_pct:
            crit.append(f"宽度{cue.get('width_pct')}%超红线≤{max_width_pct:.0f}%")
        yr = cue.get("y_range") or [0, 0]
        yb = yr[1] if len(yr) > 1 else 0
        if yb > 0 and yb < _y_threshold:
            warn.append(f"下缘{yb}px高于安全线({_y_threshold}px,距屏底"
                        f"{_video_h - yb}px<{margin_allowance}px)")
        if crit:
            violations.append({"cue": cue.get("index"),
                               "severity": "critical", "issues": crit})
        elif warn:
            violations.append({"cue": cue.get("index"),
                               "severity": "warning", "issues": warn})
    return violations


__all__ = ["hex_to_ass_color", "build_ass_style", "generate_srt",
           "generate_ass_from_srt", "escape_subtitles_path",
           "parse_srt_cues", "probe_libass", "render_subtitles_best",
           "verify_subtitles_cues", "check_subtitle_cues",
           "build_karaoke_ass_header", "generate_karaoke_ass"]
