"""Multi-round review engine with failure classification and revision strategies."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Optional

from .slideshow_risk import score_slideshow_risk
from shipin_platform.services.estimate import NARRATION_RATE_ZH


class Severity(str, Enum):
    CRITICAL = "critical"
    SUGGESTION = "suggestion"
    NITPICK = "nitpick"


class Decision(str, Enum):
    PASS = "pass"
    PASS_WITH_WARNINGS = "pass_with_warnings"
    REVISE = "revise"
    STALL = "stall"
    STOP = "stop"


@dataclass
class Finding:
    """A single issue found during review."""
    dimension: str
    severity: Severity
    issue: str
    evidence: str
    failure_mode: str
    revision_strategy: str
    proposed_fix: str = ""
    status: str = "pending"


@dataclass
class ReviewReport:
    """Complete review report for one round."""
    stage: str
    round: int
    findings: list[Finding] = field(default_factory=list)
    decision: Decision = Decision.PASS
    improvement_score: Optional[float] = None
    next_action: str = ""
    revision_plan: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def critical_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.CRITICAL)

    @property
    def suggestion_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.SUGGESTION)

    @property
    def nitpick_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == Severity.NITPICK)

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "round": self.round,
            "findings": [asdict(f) for f in self.findings],
            "decision": self.decision.value,
            "stats": {
                "critical": self.critical_count,
                "suggestion": self.suggestion_count,
                "nitpick": self.nitpick_count,
            },
            "improvement_score": self.improvement_score,
            "next_action": self.next_action,
            "revision_plan": self.revision_plan,
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class FailureClassifier:
    """Classifies review findings into failure modes for targeted revision."""

    # ── Image prompt failures ──────────────────────────────────────
    _IMAGE_PROMPT_MODES = {
        "SUBJECT_INCOMPLETE": ("subject", "缺少标志", "A1"),
        "PRONOUN_REFERENCE": ("subject", "代词指代", "A2"),
        "MOTION_VAGUE": ("motion", "模糊", "B1"),
        "CAMERA_CONFLICT": ("camera", "矛盾", "C1"),
        "STYLE_ANCHOR_MISSING": ("style", "缺失", "D1"),
        "SUBJECTIVE_WORD": ("style", "主观词", "E1"),
        "CHARACTER_INCONSISTENCY": ("consistency", "角色不一致", "A3"),
        "LIGHTING_INCONSISTENCY": ("consistency", "光线不一致", "E2"),
    }

    # ── Video prompt failures ──────────────────────────────────────
    _VIDEO_PROMPT_MODES = {
        "I2V_APPEARANCE_REPEAT": ("i2v", "重复外观", "G1"),
        "I2V_MOTION_MISSING": ("i2v", "缺少运动", "G2"),
        "I2V_END_FRAME_INCONSISTENT": ("i2v", "尾帧不一致", "G3"),
        "CAMERA_TERM_ERROR": ("camera", "术语错误", "H1"),
        "MODEL_FORMAT_ERROR": ("format", "格式错误", "I1"),
        "WORD_COUNT_EXCEEDED": ("format", "字数超限", "I2"),
        "TIMELINE_DISORDER": ("timing", "时序混乱", "H2"),
    }

    # ── Script failures ────────────────────────────────────────────
    _SCRIPT_MODES = {
        "DURATION_MISMATCH": ("duration", "时长不匹配", "S1"),
        "MISSING CHAPTER": ("structure", "章节缺失", "S2"),
        "SHOT_TOO_SHORT": ("pacing", "镜头过短", "S3"),
        "SUBJECTIVE_WORD": ("language", "主观词汇", "S4"),
        "THEN_CONNECTION": ("language", "连接词错误", "S5"),
        "NARRATION_TOO_LONG": ("length", "旁白过长", "S6"),
        "INFEASIBLE_SCENE": ("feasibility", "不可行场景", "S7"),
        "NARRATION_DUPLICATED": ("language", "旁白重复", "S8"),
        "PLACEHOLDER_LEAK": ("language", "占位符", "S9"),
    }

    # ── Storyboard failures ────────────────────────────────────────
    _STORYBOARD_MODES = {
        "SLIDESHOW_RISK": ("pacing", "幻灯片风险", "ST1"),
        "REPEATED_SHOT_SIZE": ("variation", "景别重复", "ST2"),
        "STATIC_OVERUSE": ("pacing", "静态过多", "ST3"),
        "LIGHTING_INCONSISTENCY": ("consistency", "光线不一致", "ST4"),
        "MISSING_HERO": ("hero", "缺少Hero帧", "ST5"),
        "INCOMPLETE_5ASPECT": ("completeness", "5-Aspect不完整", "ST6"),
        "ARC_INCOMPLETE": ("story", "五幕骨架缺失", "ST7"),
        "BEAT_FIELDS_MISSING": ("arc", "质感三字段缺失", "ST8"),
        "SHOT_REUSED": ("reuse", "素材复用超限", "ST9"),
        "NARRATION_DUPLICATED": ("language", "旁白重复", "ST10"),
        "CAMERA_SAME_ADJACENT": ("camera", "机位重复", "ST11"),
        "PLACEHOLDER_LEAK": ("language", "占位符", "ST12"),
    }
    # 素材复用红线的模型值(§10.7.1):同一 shot 变量 ≥3 次即危急。
    _REUSE_HARD_LIMIT = 3

    # ── Post-production failures ───────────────────────────────────
    _POSTPROD_MODES = {
        "CODEC_MISMATCH": ("technical", "编码不匹配", "PP1"),
        "RESOLUTION_ERROR": ("technical", "分辨率错误", "PP2"),
        "AUDIO_MISSING": ("audio", "音频缺失", "PP3"),
        "SUBTITLE_SYNC": ("subtitle", "字幕不同步", "PP4"),
        "DUCKING_FAILED": ("audio", "Ducking失效", "PP5"),
        "LOUDNESS_ERROR": ("audio", "响度偏差", "PP6"),
        "TRANSITION_POOR": ("transition", "转场生硬", "PP7"),
        "SUBTITLE_POSITION": ("subtitle", "字幕位置不当", "PP8"),
        "COLOR_INCONSISTENT": ("color", "色调不一致", "PP9"),
    }

    # ── Image generation failures ──────────────────────────────────
    _IMAGE_GEN_MODES = {
        "ANATOMY_ERROR": ("quality", "解剖错误", "IG1"),
        "COLOR_MISMATCH": ("quality", "颜色不符", "IG2"),
        "CONSISTENCY_FAIL": ("consistency", "一致性失败", "IG3"),
        "COMPOSITION_WEAK": ("quality", "构图弱", "IG4"),
        "MISSING_ELEMENT": ("quality", "缺失元素", "IG5"),
        "UNWANTED_ELEMENT": ("quality", "多余元素", "IG6"),
    }

    # ── Video generation failures ──────────────────────────────────
    _VIDEO_GEN_MODES = {
        "TEMPORAL_GLITCH": ("quality", "时序闪烁", "VG1"),
        "CHARACTER_DRIFT": ("consistency", "角色漂移", "VG2"),
        "ACTION_MISMATCH": ("quality", "动作不符", "VG3"),
        "BACKGROUND_DRIFT": ("quality", "背景漂移", "VG4"),
        "TECHNICAL_ERROR": ("technical", "技术错误", "VG5"),
    }

    # ── Brief failures ─────────────────────────────────────────────
    _BRIEF_MODES = {
        "MISSING_DIMENSION": ("completeness", "维度缺失", "B1"),
        "DURATION_TOO_SHORT": ("validation", "时长过短", "B2"),
        "DURATION_TOO_LONG": ("validation", "时长过长", "B3"),
        "AMBIGUOUS_TYPE": ("validation", "类型模糊", "B4"),
        "CONTRADICTION": ("validation", "内容矛盾", "B5"),
    }

    MODE_MAP = {
        "image_prompt": _IMAGE_PROMPT_MODES,
        "video_prompt": _VIDEO_PROMPT_MODES,
        "script": _SCRIPT_MODES,
        "storyboard": _STORYBOARD_MODES,
        "post_production": _POSTPROD_MODES,
        "image_gen": _IMAGE_GEN_MODES,
        "video_gen": _VIDEO_GEN_MODES,
        "brief": _BRIEF_MODES,
    }

    # Modes that are always CRITICAL regardless of the legacy endswith heuristic.
    CRITICAL_MODES = frozenset({
        "SUBJECT_INCOMPLETE", "PRONOUN_REFERENCE", "STYLE_ANCHOR_MISSING",
        "CHARACTER_INCONSISTENCY", "LIGHTING_INCONSISTENCY",
        "I2V_APPEARANCE_REPEAT", "I2V_MOTION_MISSING", "I2V_END_FRAME_INCONSISTENT",
        "CAMERA_CONFLICT", "MODEL_FORMAT_ERROR", "WORD_COUNT_EXCEEDED",
        "TIMELINE_DISORDER", "DURATION_MISMATCH", "INFEASIBLE_SCENE",
        "INCOMPLETE_5ASPECT", "CODEC_MISMATCH", "RESOLUTION_ERROR",
        "AUDIO_MISSING", "SUBTITLE_SYNC", "LOUDNESS_ERROR",
        "CONSISTENCY_FAIL", "MISSING_ELEMENT", "UNWANTED_ELEMENT",
        "TEMPORAL_GLITCH", "CHARACTER_DRIFT", "TECHNICAL_ERROR",
        "CONTRADICTION", "AMBIGUOUS_TYPE",
        "ARC_INCOMPLETE", "BEAT_FIELDS_MISSING", "SHOT_REUSED",
        "NARRATION_TOO_LONG", "REUSE_LIMIT_EXCEEDED",
        "NARRATION_DUPLICATED", "CAMERA_SAME_ADJACENT", "PLACEHOLDER_LEAK",
    })

    def classify(self, stage: str, finding_text: str, evidence: str = "",
                 severity: Optional[str] = None) -> dict:
        """Classify a finding into a failure mode and revision strategy.

        Scoring match against: Chinese keyword, normalized English mode key
        (substring), dimension name (whole word), or leading-word overlap
        (e.g. "missing_x" -> MISSING_DIMENSION). The most specific match wins,
        so "i2v_motion_missing" resolves to I2V_MOTION_MISSING, not to
        I2V_APPEARANCE_REPEAT just because both share the "i2v" dimension.
        ``severity`` (when given) always wins; otherwise CRITICAL_MODES
        membership decides, falling back to the legacy suffix heuristic.
        Returns {"mode": str, "strategy": str, "severity": str}
        """
        modes = self.MODE_MAP.get(stage, {})
        text = (finding_text + " " + evidence).lower()
        norm_text = text.replace("_", " ").replace("-", " ")
        first_word = norm_text.split()[0] if norm_text.split() else ""

        best = None  # (score, mode, dimension, keyword, strategy)
        for mode, (dimension, keyword, strategy) in modes.items():
            norm_mode = mode.lower().replace("_", " ").replace("-", " ")
            mode_words = norm_mode.split()
            score = 0
            if keyword.lower() in norm_text:
                score = max(score, len(keyword) + 50)
            if norm_mode in norm_text:
                score = max(score, len(norm_mode) + 40)
            if re.search(rf"\b{re.escape(dimension)}\b", norm_text):
                score = max(score, len(dimension) + 20)
            if first_word and mode_words and first_word == mode_words[0]:
                score = max(score, len(mode_words[0]) + 10)
            if score and (best is None or score > best[0]):
                best = (score, mode, dimension, keyword, strategy)

        if best is None:
            return {"mode": "UNKNOWN", "strategy": "MANUAL",
                    "severity": severity or "SUGGESTION"}
        _, mode, _dimension, _keyword, strategy = best
        sev = severity or (
            "CRITICAL" if mode in self.CRITICAL_MODES
            else "CRITICAL" if mode.endswith(("1", "2", "3")) or "ERROR" in mode or "FAIL" in mode
            else "SUGGESTION"
        )
        return {"mode": mode, "strategy": strategy, "severity": sev}



# 轮39:主观词词根(检查与机械修复两侧共用;前缀匹配,"震撼"/"震撼的"都中)
_SUBJECTIVE_ROOTS = (
    "震撼", "感人", "凄美", "史诗", "完美", "惊艳", "绝美", "回味无穷",
    "口感惊艳", "无与伦比", "无以伦比", "颠覆", "炸裂", "极致", "顶级",
    "尊享", "奢享", "臻品", "引领", "赋能", "叹为观止", "欲罢不能",
    "epic", "inspiring", "powerful", "beautiful", "moody",
    "cinematic", "breathtaking", "stunning", "amazing", "magical",
)

class RevisionEngine:
    """Applies revision strategies based on failure modes."""

    # Strategy implementations for common cases
    STRATEGY_TEMPLATES = {
        "A1": {  # Add missing subject features
            "description": "从character_profile复制signature_features到prompt中",
            "template": "在subject描述中添加', {feature}'到列表中",
        },
        "A2": {  # Replace pronoun reference
            "description": "将代词替换为角色名+特征描述",
            "template": "'{pronoun}' → '{character_name}, {features}'",
        },
        "B1": {  # Specific motion description
            "description": "添加具体动词+速度+幅度+时序",
            "template": "'{vague_action}' → '{specific_action},{speed} {幅度描述}'",
        },
        "C1": {  # Camera conflict resolution
            "description": "删除矛盾描述,保留一个主运动",
            "template": "删除'{conflicting_term}',保留'{primary_motion}'",
        },
        "D1": {  # Style anchor missing
            "description": "在prompt末尾添加完整style_anchor",
            "template": "在aspect ratio之前追加: '{style_anchor}'",
        },
        "E1": {  # Subjective word replacement
            "description": "将主观词替换为视觉描述",
            "templates": {
                "confident smile": "lips curved upward, eyes slightly narrowed",
                "sad": "head slightly lowered, shoulders relaxed downward",
                "happy": "lips curved upward, eyes wide and bright",
                "dramatic": "high contrast lighting, deep shadows on one side",
                "warm atmosphere": "golden hour light casting amber tones",
            },
        },
        "G1": {  # I2V appearance repeat
            "description": "删除外观描述,只保留运动描述",
            "rule": "I2V原则:图片已展示的外观不在视频提示词中重复",
        },
        "G2": {  # I2V motion missing
            "description": "添加具体的动作动词+速度+幅度",
            "rule": "每个镜头必须描述主体的运动,即使是很轻微的动作",
        },
        "H1": {  # Camera term correction
            "description": "修正镜头运动术语",
            "corrections": {
                "zoom in": "dolly in",
                "zoom out": "dolly out",
                "pan left": "truck left",
                "tilt up": "crane up",
            },
        },
        "I1": {  # Model format conversion
            "description": "转换为目标模型的正确格式结构",
            "rule": "Seedance用Shot N(...)格式,Sora用Prose+Cinematography格式",
        },
        "S1": {  # Duration mismatch
            "description": "调整旁白字数或镜头时长分配",
            "formula": "目标字数 = duration_sec × 2.67 (160字/分钟)",
        },
        "S2": {  # Missing chapter
            "description": "重新生成缺失章节",
            "rule": "使用But/Therefore方法补写缺失的章节",
        },
        "S4": {  # Subjective word in script
            "description": "替换为主观词汇",
            "rule": "描述视觉现象而非主观感受",
        },
        "ST1": {  # Slideshow risk
            "description": "添加运动描述或改变景别",
            "rules": {
                "repetition": "相邻镜头使用不同景别",
                "weak_motion": "添加镜头运动描述",
                "static_pace": "缩短单个镜头时长",
            },
        },
        "ST2": {  # Repeated shot size
            "description": "重新分配景别",
            "rule": "确保相邻镜头不使用相同景别",
        },
        "PP1": {  # Codec mismatch
            "description": "重新编码为H.264+AAC",
            "params": "-c:v libx264 -c:a aac",
        },
        "PP2": {  # Resolution error
            "description": "重新缩放+填充",
            "params": "-vf 'scale=W:H:force_original_aspect_ratio=decrease,pad=W:H:(ow-iw)/2:(oh-ih)/2'",
        },
        "PP5": {  # Ducking failed
            "description": "调整sidechaincompress参数",
            "params": "threshold=0.02→0.015, ratio=9→6",
        },
        "PP6": {  # Loudness error
            "description": "重新应用loudnorm滤镜",
            "params": "loudnorm=I=-14:TP=-1.0:LRA=11",
        },
    }

    def apply(self, stage: str, strategy: str, context: dict) -> dict:
        """Apply a revision strategy to fix the identified issues.

        Returns the revision instructions.
        """
        template = self.STRATEGY_TEMPLATES.get(strategy)
        if not template:
            return {"ok": False, "error": f"Unknown strategy: {strategy}"}

        return {
            "ok": True,
            "strategy": strategy,
            "description": template.get("description", ""),
            "instructions": template,
            "context": context,
        }

    # ── Mechanical auto-fixes ─────────────────────────────────────
    # Text fields mechanical fixes operate on, per stage:
    #   (extra_list_key_or_None, list_key, text_field_key)
    _TEXT_FIELDS = {
        "image_prompt": ("hero_prompts", "shot_prompts", "prompt_en"),
        "video_prompt": (None, "shot_prompts", "prompt_text"),
        "script": (None, "shots", "narration"),
    }

    # E1: subjective word → visual description ("" = just remove)
    # 轮39: richer entries first(视觉化改写),词根并集殿后(删除)——与检查
    # 侧 _SUBJECTIVE_ROOTS 同源,不脱节。轮40:词根按长度降序——重叠词根
    # (惊艳/口感惊艳)必须先删长词根,否则先删短的「惊艳」会让「口感惊艳」
    # 再也匹配不上,留下「这杯酸奶的口感，」这种悬空残词(六审 #5)。
    SUBJECTIVE_REPLACEMENTS = {
        "confident smile": "lips curved upward, eyes slightly narrowed",
        "sad": "head slightly lowered, shoulders relaxed downward",
        "happy": "lips curved upward, eyes wide and bright",
        "dramatic": "high contrast lighting, deep shadows on one side",
        "warm atmosphere": "golden hour light casting amber tones",
        **{root: "" for root in sorted(_SUBJECTIVE_ROOTS,
                                       key=len, reverse=True)},
    }

    def fix(self, stage: str, data: dict, report: ReviewReport) -> dict:
        """Apply mechanical fixes for auto-fixable findings.

        Returns {"data": fixed_copy, "applied": [mode...], "manual": [mode...],
        "notes": [...]}. Never mutates the input. Findings whose failure mode
        has no mechanical fix (e.g. duration mismatch needs the LLM) are listed
        in "manual" for the caller to regenerate.
        """
        fixed = copy.deepcopy(data)
        applied: list[str] = []
        manual: list[str] = []
        notes: list[str] = []

        for finding in report.findings:
            mode = finding.failure_mode
            # 轮40:同 mode 已机械修好 → 后续 finding 直接跳过。旧逻辑对
            # 同 mode 的每条 finding 都重跑修复器:第一条修成功(applied),
            # 后续已是 no-op(ok=False)又落 manual——manual_modes 是对外
            # 合约(每一项要求 LLM 重生),"已修好又被要求重写"白烧一轮
            # 迭代且有改写回归风险(六审 #5:同段三个主观词根即触发)。
            if mode in applied:
                continue
            ok = False
            try:
                if mode == "STYLE_ANCHOR_MISSING":
                    ok = self._fix_style_anchor(fixed)
                elif mode == "SUBJECTIVE_WORD":
                    ok = self._fix_subjective(fixed, stage)
                elif mode == "CAMERA_TERM_ERROR":
                    ok = self._fix_camera_terms(fixed)
                elif mode == "I2V_APPEARANCE_REPEAT":
                    ok = self._fix_i2v_appearance(fixed)
                elif mode == "NARRATION_TOO_LONG":
                    ok = self._fix_narration_budget(fixed)
                elif mode == "WORD_COUNT_EXCEEDED":
                    ok = self._fix_prompt_word_count(fixed)
            except Exception as e:  # a broken fix must never break the loop
                notes.append(f"{mode}: fix error: {e}")
                ok = False
            bucket = applied if ok else manual
            if mode not in bucket:
                bucket.append(mode)

        return {"data": fixed, "applied": applied, "manual": manual, "notes": notes}

    def _fix_narration_budget(self, data: dict) -> bool:
        """NARRATION_TOO_LONG 的确定性修复:按预算等比裁剪每镜旁白。

        real-guard-10 实证:旁白 60 字 vs 预算 53 字,该 finding 每轮都
        机械地交给 LLM 重生,而重生又超字数 → 4 轮不收敛。字数裁剪是
        纯机械操作(保留句尾标点、截断超长句),在此处直接修完。
        """
        shots = data.get("shots") or []
        texts = [s.get("narration") for s in shots
                 if isinstance(s.get("narration"), str)]
        total = sum(len(t) for t in texts)
        if not total:
            return False
        duration = data.get("duration_sec", 0)
        if isinstance(duration, str):
            try:
                duration = float(duration)
            except ValueError:
                duration = 0
        # 字数预算唯一数据源:services/estimate.NARRATION_RATE_ZH(原散落 2.67)
        budget = duration * NARRATION_RATE_ZH
        if not budget or total <= budget:
            return False
        # 目标取预算的 95%,留出余量避免边界抖动
        target = int(budget * 0.95)
        changed = False
        for s in shots:
            t = s.get("narration")
            if not isinstance(t, str) or not t:
                continue
            share = max(1, round(len(t) / total * target))
            if len(t) > share:
                cut = t[:share]
                # 截在最后一个完整停顿(,。!?、)之后,保持可读
                last_punct = max(cut.rfind(p) for p in ",。!?、,")
                if last_punct >= max(4, share - 6):
                    cut = cut[:last_punct + 1]
                if cut != t:
                    s["narration"] = cut
                    changed = True

        # 单句上限（MAX_NARRATION_CHARS=14）：预算裁剪后单句仍可能 >14 字，
        # 该 finding 无机械修复会每轮推给 LLM → 纯 LLM 循环不收敛。
        # 在停顿处切短，保持“一句一个点”的短句标准。
        for s in shots:
            t = s.get("narration")
            if not isinstance(t, str) or len(t) <= 14:
                continue
            cut = t[:14]
            last_punct = max(cut.rfind(pc) for pc in "，。！？、,")
            if last_punct >= 4:
                cut = cut[:last_punct + 1]
            if cut != t:
                s["narration"] = cut
                changed = True
        return changed

    def _fix_prompt_word_count(self, data: dict) -> bool:
        """WORD_COUNT_EXCEEDED(I2)的确定性修复:video_prompt 超 380 字
        逐镜裁剪。

        轮59(一句话驱动实测):LLM 按 VIDEO_PROMPT 模板生成的提示词系统
        性超限(实测 406/386/388 字),而 I2 没有机械修复器——每轮只把
        critical 交回 LLM 重写,重写又超 → 3 轮不收敛 stall,generate
        在文本阶段就卡死(用户一句话到出片路径断在这里)。
        裁剪策略:优先删可再生的修饰从句(非句式核心),在 380 内保留
        句式关键段(动作/机位/光线/禁止形变子句);保底按整句边界截断。
        """
        limit = self.VIDEO_PROMPT_MAX_CHARS if hasattr(
            self, "VIDEO_PROMPT_MAX_CHARS") else 380
        changed = False
        for key in ("shot_prompts", "prompts"):
            items = data.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                text = item.get("prompt_text")
                if not isinstance(text, str) or len(text) <= limit:
                    continue
                cut = text[:limit]
                # 优先在最后一个完整句边界(英文句号/分号)前收,避免截断
                # 从句半句;边界太靠前(<60%)则退回空格边界,再保底硬截
                edge = max(cut.rfind("."), cut.rfind(";"))
                if edge >= int(limit * 0.6):
                    cut = cut[:edge + 1]
                else:
                    sp = cut.rfind(" ")
                    if sp >= int(limit * 0.6):
                        cut = cut[:sp]
                if cut != text:
                    item["prompt_text"] = cut
                    changed = True
        return changed

    def _iter_text_items(self, data: dict, stage: str):
        """Yield (item_dict, field_key) for every text field the stage owns."""
        spec = self._TEXT_FIELDS.get(stage)
        if not spec:
            return
        extra_key, list_key, field_key = spec
        for key in filter(None, (extra_key, list_key)):
            for item in data.get(key, []):
                if isinstance(item, dict) and isinstance(item.get(field_key), str):
                    yield item, field_key

    def _fix_style_anchor(self, data: dict) -> bool:
        anchor = data.get("style_anchor", "")
        if not anchor:
            return False
        changed = False
        for item, field_key in self._iter_text_items(data, "image_prompt"):
            if anchor not in item[field_key]:
                item[field_key] = item[field_key].rstrip() + ", " + anchor
                changed = True
        return changed

    def _fix_subjective(self, data: dict, stage: str) -> bool:
        changed = False
        for item, field_key in self._iter_text_items(data, stage):
            text = item[field_key]
            item_changed = False
            for phrase, replacement in self.SUBJECTIVE_REPLACEMENTS.items():
                if phrase.isascii():
                    pattern = re.compile(re.escape(phrase), re.IGNORECASE)
                    if pattern.search(text):
                        text = pattern.sub(replacement, text)
                        item_changed = True
                elif phrase in text:
                    text = text.replace(phrase, replacement)
                    item_changed = True
            if item_changed:
                item[field_key] = self._cleanup(text)
                changed = True
        return changed

    def _fix_camera_terms(self, data: dict) -> bool:
        corrections = self.STRATEGY_TEMPLATES.get("H1", {}).get("corrections", {})
        if not corrections:
            return False
        changed = False
        for item, field_key in self._iter_text_items(data, "video_prompt"):
            text = item[field_key]
            item_changed = False
            for wrong, right in corrections.items():
                pattern = re.compile(re.escape(wrong), re.IGNORECASE)
                if pattern.search(text):
                    text = pattern.sub(right, text)
                    item_changed = True
            if item_changed:
                item[field_key] = text
                changed = True
        return changed

    def _fix_i2v_appearance(self, data: dict) -> bool:
        # ReviewEngine is defined later in this module; resolve at call time.
        appearance_re = ReviewEngine._APPEARANCE_RE
        changed = False
        for item, field_key in self._iter_text_items(data, "video_prompt"):
            text = appearance_re.sub("", item[field_key])
            if text != item[field_key]:
                item[field_key] = self._cleanup(text)
                changed = True
        return changed

    @staticmethod
    def _cleanup(text: str) -> str:
        text = re.sub(r"\s+([,.;:!?、,。;:!?])", r"\1", text)
        text = re.sub(r"([,;:])(?=\S)", r"\1 ", text)
        text = re.sub(r"(?:\s*,\s*)+", ", ", text)
        text = re.sub(r" {2,}", " ", text)
        return text.strip(" ,;")


# 轮36:重复旁白的归一化键——空白+中英文标点全剥离。旧逻辑只去空白,
# 「每一杯都是匠心」vs「每一杯，都是匠心」只差标点被判为不同 key 放行,
# 配音照念两遍(五审 #6c)。
_NARR_PUNCT_RE = re.compile(
    "[" + re.escape("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
    + r"\s\u3000-\u303f\uff00-\uffef\u2014\u2018\u2019\u201c\u201d"
    + r"\u2026\u00b7]+")


def _norm_narration_key(text: str) -> str:
    return _NARR_PUNCT_RE.sub("", str(text or "")).lower()



class ReviewEngine:
    """Main review engine that orchestrates multi-round iteration."""

    # TVC 短句大字标准(§10.7.1):旁白一句 ≤14 字
    MAX_NARRATION_CHARS = 14
    # 视频提示词字数上限(I2):超过此长度视频模型丢失指令遵循
    VIDEO_PROMPT_MAX_CHARS = 380
    # 素材复用红线(§10.7.1 第4条):同一镜头全片 ≤3 次
    _REUSE_HARD_LIMIT = 3

    def __init__(self, config=None):
        self.classifier = FailureClassifier()
        self.revision = RevisionEngine()
        self.config = config or {}

    def run_review(
        self,
        stage: str,
        content: dict,
        round_num: int = 1,
        previous_report: Optional[ReviewReport] = None,
        fix_applied: bool = False,
    ) -> ReviewReport:
        """Run one round of review on content.

        Args:
            stage: Stage name (brief/script/storyboard/image_prompt/image_gen/video_prompt/video_gen/post_production)
            content: The content to review (dict)
            round_num: Current round number
            previous_report: Previous review report (for improvement calculation)
            fix_applied: Whether mechanical fixes were applied since last round
                (a round with zero improvement but real fixes is REVISE, not STALL)

        Returns:
            ReviewReport with findings and decision
        """
        findings = self._review_stage(stage, content)
        stats = {
            "critical": sum(1 for f in findings if f.severity == Severity.CRITICAL),
            "suggestion": sum(1 for f in findings if f.severity == Severity.SUGGESTION),
            "nitpick": sum(1 for f in findings if f.severity == Severity.NITPICK),
        }

        # Calculate improvement
        improvement = None
        if previous_report:
            prev_critical = previous_report.critical_count
            if prev_critical > 0:
                improvement = (prev_critical - stats["critical"]) / prev_critical

        # Decision
        max_rounds = self.config.get("max_rounds", {}).get(stage, 3)
        decision = self._decide(stats, round_num, improvement,
                                max_rounds=max_rounds, fix_applied=fix_applied)
        next_action = self._next_action(decision, stage, round_num)

        report = ReviewReport(
            stage=stage,
            round=round_num,
            findings=findings,
            decision=decision,
            improvement_score=improvement,
            next_action=next_action,
            metadata={"max_rounds": max_rounds},
        )
        report.revision_plan = self._build_revision_plan(stage, findings, decision)
        return report

    def _build_revision_plan(self, stage: str, findings: list[Finding],
                             decision: Decision) -> list[str]:
        """Compress every finding into one ordered, actionable revision plan.

        The plan is the single contract the caller hands back to the LLM:
        critical items first ("必改"), each with an explicit "怎么改" direction;
        suggestions after; and a lock line telling the LLM what it must NOT
        touch. An empty plan means nothing to fix.
        """
        criticals = [f for f in findings if f.severity == Severity.CRITICAL]
        suggestions = [f for f in findings if f.severity not in
                       (Severity.CRITICAL, Severity.NITPICK)]
        nitpicks = [f for f in findings if f.severity == Severity.NITPICK]
        lines: list[str] = []

        for i, f in enumerate(criticals, 1):
            how = f.proposed_fix or f.revision_strategy or "按该项规则重写"
            lines.append(f"必改{i} · {f.dimension} · {f.issue} → 怎么改：{how}")
        for i, f in enumerate(suggestions, 1):
            how = f.proposed_fix or f.revision_strategy or "按该项规则优化"
            lines.append(f"建议{i} · {f.dimension} · {f.issue} → 怎么改：{how}")
        for i, f in enumerate(nitpicks, 1):
            lines.append(f"细节{i} · {f.dimension} · {f.issue}")

        if decision in (Decision.REVISE, Decision.STOP, Decision.STALL):
            if criticals:
                lines.append("锁项:只允许修改上述必改/建议/细节条目对应的字段,"
                             "其余内容保持原样不动;改完把完整 JSON 原样返回。")
            else:
                lines.append("锁项:无必改项,按列出的建议微调即可,其余内容不动。")
        return lines

    def _review_stage(self, stage: str, content: dict) -> list[Finding]:
        """Review content for a specific stage. Returns list of findings."""
        findings = []

        if stage == "brief":
            findings.extend(self._review_brief(content))
        elif stage == "script":
            findings.extend(self._review_script(content))
        elif stage == "storyboard":
            findings.extend(self._review_storyboard(content))
        elif stage == "image_prompt":
            findings.extend(self._review_image_prompt(content))
        elif stage == "video_prompt":
            findings.extend(self._review_video_prompt(content))
        # image_gen, video_gen, post_production reviewed externally via VLM/ffprobe

        return findings

    def _review_brief(self, brief: dict) -> list[Finding]:
        """Review brief completeness."""
        findings = []
        required = ["content_type", "product_info", "target_platform",
                     "duration_sec", "target_audience", "tone",
                     "creative_direction", "reference_materials", "special_requirements"]
        # 平台文档(BRIEF_DIMENSIONS)明确「可为空字符串」的两个维度:
        # key 存在即视为完整,空串不算缺失--文档与引擎必须一致。
        empty_ok = {"reference_materials", "special_requirements"}

        for field in required:
            value = brief.get(field)
            if field in empty_ok:
                if field not in brief:
                    missing = True
                else:
                    missing = False
            else:
                missing = not value
            if missing:
                cls = self.classifier.classify("brief", f"missing_{field}", field)
                findings.append(Finding(
                    dimension=field,
                    severity=Severity.CRITICAL,
                    issue=f"缺失维度: {field}",
                    evidence=f"brief中没有{field}字段",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix=f"向用户追问{field}",
                ))

        duration = brief.get("duration_sec", 0)
        if isinstance(duration, str):
            try:
                duration = float(duration)
            except ValueError:
                duration = 0
        if isinstance(duration, (int, float)) and duration and duration < 15:
            cls = self.classifier.classify("brief", "duration_too_short", str(duration))
            findings.append(Finding(
                dimension="duration",
                severity=Severity.CRITICAL,
                issue=f"时长{duration}秒太短",
                evidence=f"duration_sec={duration}",
                failure_mode=cls["mode"],
                revision_strategy=cls["strategy"],
                proposed_fix="建议用户确认或扩展到15秒以上",
            ))

        return findings

    def _review_script(self, script: dict) -> list[Finding]:
        """Review script quality."""
        findings = []
        shots = script.get("shots", [])
        total_duration = script.get("duration_sec", 0)
        if isinstance(total_duration, str):
            try:
                total_duration = float(total_duration)
            except ValueError:
                total_duration = 0
        narration_words = sum(len(s.get("narration", "")) for s in shots
                              if isinstance(s.get("narration"), str))
        max_words = total_duration * NARRATION_RATE_ZH

        # Duration check
        # 轮57(真实使用发现,子智能体 A):旧实现是双向 ±10% 带
        # (abs(words-budget) > budget*0.1)——旁白**少于**预算 15% 也判
        # critical,而文案只说「调整至 N 字以内」:34 字 vs 40 字预算被
        # 打死,机械修复把句子截成病句,round2 stall 死锁(建议的修复
        # 在 34 字时已满足条件)。改成方向明确的两支:
        #   超预算 → critical NARRATION_TOO_LONG(名字即语义);
        #   低于预算 → suggestion NARRATION_SPARSE(旁白少不是缺陷,
        #        只提示,不 stall——最没信息量的答案不受最重的罚)。
        if max_words > 0 and narration_words > max_words * 1.1:
            cls = self.classifier.classify("script", "duration_mismatch", "")
            findings.append(Finding(
                dimension="duration",
                severity=Severity.CRITICAL,
                issue=f"旁白{narration_words}字 vs 预算{int(max_words)}字(超预算)",
                evidence=f"narration_words={narration_words}, max_words={int(max_words)}",
                failure_mode=cls["mode"],
                revision_strategy=cls["strategy"],
                proposed_fix=f"精简旁白至{int(max_words)}字以内",
            ))
        elif max_words > 0 and narration_words < max_words * 0.9:
            cls = self.classifier.classify("script", "duration_mismatch", "")
            findings.append(Finding(
                dimension="duration",
                severity=Severity.SUGGESTION,
                issue=f"旁白{narration_words}字 vs 预算{int(max_words)}字(偏少)",
                evidence=f"narration_words={narration_words}, max_words={int(max_words)}",
                failure_mode=cls["mode"],
                revision_strategy=cls["strategy"],
                proposed_fix=(f"旁白少于预算 10% 以上,信息量可能不足;"
                              f"如叙事完整可忽略(不阻断)"),
            ))

        # Subjective words check
        # 轮39:旧表 12 个完整形态硬编码("震撼的"命中、"震撼"漏)、且不收
        # 中文广告高频主观词("口感惊艳""回味无穷"零 finding)。改词根前缀
        # 匹配:词根出现即中,修复表(RevisionEngine.SUBJECTIVE_REPLACEMENTS)
        # 以同一词根并集扩展,两侧不脱节。
        narration_text = " ".join(s.get("narration", "") for s in shots
                                  if isinstance(s.get("narration"), str))
        for root in _SUBJECTIVE_ROOTS:
            if root.lower() in narration_text.lower():
                cls = self.classifier.classify("script", "subjective_word", root)
                findings.append(Finding(
                    dimension="language",
                    severity=Severity.SUGGESTION,
                    issue=f"使用主观词'{root}'",
                    evidence=f"出现在旁白中",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="替换为视觉描述",
                ))

        # Shot too short (< 1.0s is below the perceivable TVC floor)
        for s in shots:
            dur = s.get("duration_sec")
            if isinstance(dur, str):
                try:
                    dur = float(dur)
                except ValueError:
                    dur = None
            if isinstance(dur, (int, float)) and dur < 1.0 and dur > 0:
                cls = self.classifier.classify("script", "shot_too_short", str(dur))
                findings.append(Finding(
                    dimension="pacing",
                    severity=Severity.CRITICAL,
                    issue=f"镜头{s.get('shot_id', '?')}时长{dur}s低于1秒视觉下限",
                    evidence=f"duration_sec={dur}",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix=f"把该镜头延长到 ≥1.0s(或与相邻镜头合并)",
                ))

        # Run-on connector "然后" (weak ad pacing)
        for s in shots:
            narration = s.get("narration", "")
            if isinstance(narration, str) and "然后" in narration:
                cls = self.classifier.classify("script", "then_connection", "然后")
                findings.append(Finding(
                    dimension="language",
                    severity=Severity.CRITICAL,
                    issue=f"镜头{s.get('shot_id', '?')}旁白用了口头连接词'然后'",
                    evidence=f"旁白:{narration[:60]}",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="拆句:把'然后'换成'接着/随之/而',或直接断句并列",
                ))

        # Narration short-sentence floor (§10.7 第4条细化为 §10.7.1):
        # 一句 > MAX_NARRATION_CHARS 字就进字幕区变"作文",TVC 必须短句。
        for s in shots:
            narration = s.get("narration", "")
            if not isinstance(narration, str) or not narration.strip():
                continue
            for sent in re.split(r"[。!?!?;;]", narration):
                sent = sent.strip().strip(",, ")
                if len(sent) > self.MAX_NARRATION_CHARS:
                    cls = self.classifier.classify("script", "narration_too_long", str(len(sent)))
                    findings.append(Finding(
                        dimension="length",
                        severity=Severity.CRITICAL,
                        issue=f"镜头{s.get('shot_id', '?')}旁白单句{len(sent)}字超{self.MAX_NARRATION_CHARS}字上限",
                        evidence=f"句:{sent[:60]}",
                        failure_mode=cls["mode"],
                        revision_strategy=cls["strategy"],
                        proposed_fix=f"把该句拆成多句或删减到≤{self.MAX_NARRATION_CHARS}字(短句大字标准,一句一个点)",
                    ))

# Infeasible scene keywords (un-producible effect claims)
        INFEASIBLE = ("穿越", "瞬移", "魔法", "法术", "超能力", "原地起飞", "长生",
                      "外星", "钢铁侠", "现实扭曲", "变出钱来")
        for s in shots:
            text = str(s.get("narration", "")) + str(s.get("scene", ""))
            hit = next((k for k in INFEASIBLE if k in text), None)
            if hit:
                cls = self.classifier.classify("script", "infeasible_scene", hit)
                findings.append(Finding(
                    dimension="feasibility",
                    severity=Severity.CRITICAL,
                    issue=f"镜头{s.get('shot_id', '?')}出现不可实拍的特效词'{hit}'",
                    evidence=f"含'{hit}'的文案/场景描述",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="换成可实拍/可生成的等效视觉(如:用高速镜头表现'瞬间到位')",
                ))

        # ── 台词门（§10.7 台词规范落地）：台词不能全片缺席、不能超长、
        #    不能与旁白重复、role_code 必须合法。
        valid_roles = {"hero_male", "colleague_male", "assistant_female"}
        dlg_shots = 0
        for s in shots:
            d = s.get("dialogue")
            if not isinstance(d, dict) or not d.get("text"):
                continue
            dlg_shots += 1
            text = str(d["text"]).strip()
            if len(text) > 20:
                cls = self.classifier.classify("script", "dialogue_too_long", str(len(text)))
                findings.append(Finding(
                    dimension="dialogue", severity=Severity.CRITICAL,
                    issue=f"镜头{s.get('shot_id', '?')}台词{len(text)}字超20字上限",
                    evidence=f"台词:{text[:60]}",
                    failure_mode=cls["mode"], revision_strategy=cls["strategy"],
                    proposed_fix="把台词拆短到 ≤20 字(一句台词一个意思)",
                ))
            # 轮37:旧规则只比 text.split("，")[0] in narration——首子句
            # 之后的分句与旁白逐字重复(或仅标点不同)完全逃逸。改任一
            # 分句(归一化后 ≥4 字,避开"好的"类 trivial 匹配)或整句互相
            # 包含即报。注:语义级复述(换了字复述同一信息)是 LLM 审片
            # rubric 的职责,字面层到这里为止。
            _nnorm = _norm_narration_key(str(s.get("narration", "")))
            _dnorm = _norm_narration_key(text)
            _clauses = [c for c in re.split(r"[，。！？；、,.!?;]", text)
                        if len(_norm_narration_key(c)) >= 4]
            _dupe = bool(_nnorm) and (
                any(_norm_narration_key(c) in _nnorm for c in _clauses)
                or (len(_dnorm) >= 4
                    and (_dnorm in _nnorm or _nnorm in _dnorm)))
            if _dupe:
                cls = self.classifier.classify("script", "dialogue_dupe_narration", text[:16])
                findings.append(Finding(
                    dimension="dialogue", severity=Severity.CRITICAL,
                    issue=f"镜头{s.get('shot_id', '?')}台词与旁白重复同一信息",
                    evidence=f"台词:{text[:40]} / 旁白:{s.get('narration','')[:40]}",
                    failure_mode=cls["mode"], revision_strategy=cls["strategy"],
                    proposed_fix="同一信息二选一:台词说给画面里开口说话的角色,旁白说画外音;两者不得复述同一句话",
                ))
            role = str(d.get("role_code") or "")
            if role not in valid_roles:
                cls = self.classifier.classify("script", "dialogue_role_invalid", role or "empty")
                findings.append(Finding(
                    dimension="dialogue", severity=Severity.CRITICAL,
                    issue=f"镜头{s.get('shot_id', '?')}台词 role_code 非法: {role or '空'}",
                    evidence=f"role_code={role!r} (合法集: {sorted(valid_roles)})",
                    failure_mode=cls["mode"], revision_strategy=cls["strategy"],
                    proposed_fix=f"role_code 取 {sorted(valid_roles)} 之一; 若角色名不在池内, 改由旁白承担该句",
                ))
            if s.get("shot_id") == shots[-1].get("shot_id"):
                cls = self.classifier.classify("script", "dialogue_in_outro", role or "")
                findings.append(Finding(
                    dimension="dialogue", severity=Severity.CRITICAL,
                    issue=f"末镜落版镜不应有角色台词(品牌落版用旁白/字幕)",
                    evidence=f"末镜台词:{text[:40]}",
                    failure_mode=cls["mode"], revision_strategy=cls["strategy"],
                    proposed_fix="把末镜台词移给倒数第二镜, 末镜只留旁白/品牌字幕",
                ))
        if len(shots) >= 4 and dlg_shots < 2:
            cls = self.classifier.classify("script", "dialogue_missing", str(dlg_shots))
            findings.append(Finding(
                dimension="dialogue", severity=Severity.CRITICAL,
                issue=f"全片{dlg_shots}镜有台词, 少于 2 镜——低于对白门禁",
                evidence=f"dialogue shots: {dlg_shots} / {len(shots)}",
                failure_mode=cls["mode"], revision_strategy=cls["strategy"],
                proposed_fix="给至少 2 镜加 dialogue(角色开口说话, 内容口语短句, ≤20 字), 与旁白信息互补不重复",
            ))

        # ── 抽象审核 · 旁白跨镜重复 ─────────────────────────────────
        # e2e-f3e40f87 实证:同一句旁白(如"暖灯、木质、咖啡香")被两镜
        # 原样复用,审核连过五关漏进成片——配音会念两遍,观众直接出戏。
        # 一句旁白全片只允许出现一次,逐字重复即 critical。
        seen_narration: dict[str, list[str]] = {}
        for s in shots:
            n = s.get("narration")
            if not isinstance(n, str) or not n.strip():
                continue
            key = _norm_narration_key(n)
            seen_narration.setdefault(key, []).append(
                str(s.get("shot_id") or "?"))
        for key, ids in seen_narration.items():
            if len(ids) >= 2:
                cls = self.classifier.classify(
                    "script", "narration_duplicated", ids[0])
                findings.append(Finding(
                    dimension="language", severity=Severity.CRITICAL,
                    issue=f"旁白「{key[:40]}」在镜头 {', '.join(ids)} 重复出现——同一句配音要念两遍",
                    evidence=f"shots with identical narration: {ids}",
                    failure_mode=cls["mode"], revision_strategy=cls["strategy"],
                    proposed_fix="保留一处,另一镜改写为语义不同、节奏相衬的新旁白(一句一个信息点)",
                ))

        # ── 抽象审核:占位符泄漏 ─────────────────────────────────────
        # brief 里"XX咖啡"这类占位符一旦穿过剧本/分镜直接进旁白,成品
        # 字幕与配音都会念出"XX"——人工一看就"不正常",规则必须拦下。
        def _placeholder_hit(text: str, patterns: tuple) -> str:
            for pat in patterns:
                if re.search(pat, text):
                    return pat
            return ""

        _PLACEHOLDER_PATTERNS = (r"XX\d*|xx\d*", r"占位|placeholder|TBD|TODO",
                                 r"\{[\u4e00-\u9fff:：]{1,12}\}", r"[【】]",
                                 r"<品牌|\[品牌")
        for s in shots:
            samples = {
                "narration": s.get("narration", ""),
                "dialogue": str((s.get("dialogue") or {}).get("text", ""))
                if isinstance(s.get("dialogue"), dict) else s.get("dialogue", ""),
            }
            for field, text in samples.items():
                if not isinstance(text, str):
                    continue
                hit = _placeholder_hit(text, _PLACEHOLDER_PATTERNS)
                if hit:
                    cls = self.classifier.classify(
                        "script", "placeholder_leak", hit)
                    findings.append(Finding(
                        dimension="language", severity=Severity.CRITICAL,
                        issue=f"镜头{s.get('shot_id', '?')}的{field}含占位符'{hit}'——会原样进配音/字幕",
                        evidence=f"{field}: {str(text)[:60]}",
                        failure_mode=cls["mode"], revision_strategy=cls["strategy"],
                        proposed_fix="替换为真实品牌名/产品名等具体内容,禁止把模板占位符带入台词",
                    ))

        return findings

    def _review_storyboard(self, storyboard: dict) -> list[Finding]:
        """Review storyboard completeness."""
        findings = []
        shots = storyboard.get("shots", storyboard.get("scenes", []))

        for shot in shots:
            # Check 5-Aspect completeness (tolerates both `subject` and `subject_en`)
            for aspect in ["subject", "motion", "scene", "spatial", "camera"]:
                if not self._field(shot, aspect):
                    cls = self.classifier.classify("storyboard", "incomplete_5aspect", aspect)
                    findings.append(Finding(
                        dimension=aspect,
                        severity=Severity.CRITICAL,
                        issue=f"镜头{shot.get('shot_id', shot.get('shotNum', '?'))}缺少{aspect}描述",
                        evidence=f"shot.{aspect}(_en) is empty",
                        failure_mode=cls["mode"],
                        revision_strategy=cls["strategy"],
                        proposed_fix=f"补充{aspect}维度的描述",
                    ))

        # Adjacent same shot-size (ST2) - pacing killer in 15-60s ads
        sizes = [self._canonical_shot_size(s.get("shot_size") or self._field(s, "shot_size"))
                 for s in shots]
        for i in range(1, len(sizes)):
            if sizes[i] and sizes[i] == sizes[i - 1]:
                cls = self.classifier.classify("storyboard", "repeated_shot_size", sizes[i])
                findings.append(Finding(
                    dimension="variation",
                    severity=Severity.CRITICAL,
                    issue=f"镜头{shots[i].get('shot_id', i + 1)}与上一镜同为'{sizes[i]}'景别",
                    evidence=f"consecutive shot_size='{sizes[i]}'",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="相邻镜头景别必须差异化(如特写↔全景交替)",
                ))

        # Hero frame must exist once the board has real length (TVC = 8-12 镜)
        has_hero = bool(storyboard.get("hero_shot")) or any(
            "hero" in str(shot.get("role") or shot.get("shot_id") or "").lower()
            for shot in shots)
        if len(shots) >= 4 and not has_hero:
            cls = self.classifier.classify("storyboard", "missing_hero", "hero")
            findings.append(Finding(
                dimension="hero",
                severity=Severity.CRITICAL,
                issue="分镜缺少 Hero 帧:没有产品/主角的专属视觉高潮镜头",
                evidence="no shot_id/role contains 'hero'; hero_shot absent",
                failure_mode=cls["mode"],
                revision_strategy=cls["strategy"],
                proposed_fix="指定一个镜头为 hero(shot_id 或 role 注明 hero),放产品最佳美学角度",
            ))

        # Slideshow risk (integrate existing scorer; never crashes the review)
        try:
            risk = score_slideshow_risk(shots)
            if risk.get("verdict") in ("revise", "fail"):
                cls = self.classifier.classify("storyboard", "slideshow_risk", risk.get("verdict", ""))
                findings.append(Finding(
                    dimension="pacing",
                    severity=Severity.CRITICAL,
                    issue=f"幻灯片风险 {risk.get('average', 0):.2f}/5({risk.get('verdict')})",
                    evidence=f"dimensions={risk.get('dimensions', {})}",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="按风险维度修:景别穿插/加入运镜/压缩静态镜头时长",
                ))
        except Exception:
            pass  # scorer needs fields we don't have - not a finding

        # ── No.3 hard gate: 五幕因果骨架 (§10.7.1) ─────────────────
        # 曾经的坑:storyboard 只是素材罗列,没有钩子/痛点/落版 = "拼接感"。
        # 此门是确定性规则,不依赖 LLM 自评:三拍缺任一 → CRITICAL → REVISE。
        beat_marks = {"hook": [], "pain": [], "turn": [], "outro": []}
        for i, shot in enumerate(shots):
            b = str(shot.get("beat") or "").strip().lower()
            if not b:
                continue
            if "钩" in b or "hook" in b:
                beat_marks["hook"].append(i)
            if "痛" in b or "pain" in b:
                beat_marks["pain"].append(i)
            if "转" in b or "turn" in b:
                beat_marks["turn"].append(i)
            if "落" in b or "out" in b or "版" in b or "束" in b or "收" in b:
                beat_marks["outro"].append(i)

        if len(shots) >= 4:
            for key, zh in (("hook", "钩子"), ("pain", "痛点/冲突"),
                            ("turn", "转折"), ("outro", "落版/收束")):
                if not beat_marks[key]:
                    cls = self.classifier.classify("storyboard", "arc_incomplete", zh)
                    findings.append(Finding(
                        dimension="story",
                        severity=Severity.CRITICAL,
                        issue=f"五幕弧线缺「{zh}」拍:全片{len(shots)}镜无任何shot带 beat='{zh}'(纯素材罗列病根)",
                        evidence=f"beat fields present: { {k: v for k, v in beat_marks.items()} }",
                        failure_mode=cls["mode"],
                        revision_strategy=cls["strategy"],
                        proposed_fix=f"为对应镜头补 beat 字段(钩子=开篇反常细节、痛点=日常冲突、转折=产品介入改变状态、落地=结尾大字落版);不要给每镜都标全,每拍至少1镜",
                    ))

        # ── Hard gate:TVC 质感三字段(§10.7 执行顺序1)──────────────
        # 每镜必须能回答:这镜是什么拍(beat)/什么节奏(rhythm)/什么声音(sfx)。
        # 全片缺字段=不可验收;部分镜缺=严重(混合新旧会导致节奏碎片)。
        missing_any = 0
        for i, shot in enumerate(shots):
            lacks = [f for f in ("beat", "rhythm", "sfx") if not self._field(shot, f)]
            if lacks:
                missing_any += len(lacks)
                if len(shots) > 3:
                    cls = self.classifier.classify("storyboard", "beat_fields_missing", ",".join(lacks))
                    findings.append(Finding(
                        dimension="arc",
                        severity=Severity.CRITICAL,
                        issue=f"镜头{shot.get('shot_id', i + 1)}缺质感字段 {','.join(lacks)}",
                        evidence=f"has fingerprint keys: {sorted(shot.keys())[:8]}",
                        failure_mode=cls["mode"],
                        revision_strategy=cls["strategy"],
proposed_fix="补齐 beat/rhythm/sfx 三字段(TVC 质感必备,AGENT_GUIDE §10.7)",
                ))

        # ── Hard gate:素材复用红线(§10.7.1 第4条)───────────────
        # 同一 shot_id(含 shotNum/subject 别名)在全片出现次数 >3 即危急;
        # 第2次复用必须与第1次间隔 ≥3 镜头(回环允许,滥用不允)。
        seen: dict[str, list[int]] = {}
        for i, shot in enumerate(shots):
            key = str(shot.get("shot_id") or shot.get("shotNum") or "") or \
                  str(shot.get("subject") or "")[:12]
            if not key or key == "None":
                continue
            seen.setdefault(key, []).append(i)
        for key, idxs in seen.items():
            if len(idxs) > self._REUSE_HARD_LIMIT:
                cls = self.classifier.classify("storyboard", "shot_reused", key)
                findings.append(Finding(
                    dimension="reuse",
                    severity=Severity.CRITICAL,
                    issue=f"镜头'{key[:30]}'全片出现{len(idxs)}次,超过复用红线(≤{self._REUSE_HARD_LIMIT})",
                    evidence=f"indices={idxs}",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="同一素材/镜头只允许回环复用1次且间隔≥20s;删除重复出现的镜段或更换视点/景别",
                ))
            elif len(idxs) == 2 and idxs[1] - idxs[0] <= 2:
                cls = self.classifier.classify("storyboard", "shot_reused", key)
                findings.append(Finding(
                    dimension="reuse",
                    severity=Severity.CRITICAL,
                    issue=f"片段'{key[:30]}'第2次复用距第1次仅{idxs[1] - idxs[0]}个镜头(间隔≥3镜才允许回环复用)",
                    evidence=f"indices={idxs}",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="第2次复用仅用于开篇↔收束的回环;紧贴第1次出现=拼接感,请换成其他镜头",
                ))

        # ── Hard gate:每镜必须自带 narration ───────────────────────
        # coffee-v5 教训:LLM 审查要求拆镜时剧本/分镜会分叉(7镜 vs 9镜),
        # 配音与字幕必须有唯一权威数据源 = 分镜逐镜 narration。
        for shot in shots:
            narr = shot.get("narration")
            if not isinstance(narr, str) or not narr.strip():
                cls = self.classifier.classify("storyboard", "narration_missing", "")
                findings.append(Finding(
                    dimension="completeness",
                    severity=Severity.CRITICAL,
                    issue=f"镜头{shot.get('shot_id', shot.get('shotNum', '?'))}缺 narration 字段",
                    evidence="storyboard shot has no narration (TTS/字幕数据源)",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="为该镜补 narration(一句 ≤14 字);剧本/分镜必须同步改并保持镜数一致",
                ))

# ── Soft gate:收束落版必须存在于末段(§10.7 第3条)──────────
        if len(shots) >= 4:
            last3 = shots[-3:]
            has_outro = any(
                "落" in str(s.get("beat") or "") or "out" in str(s.get("beat") or "").lower()
                for s in last3)
            if not has_outro and not (beat_marks.get("outro") and
                                      max(beat_marks["outro"]) >= len(shots) - 3):
                cls = self.classifier.classify("storyboard", "arc_incomplete", "outro_position")
                findings.append(Finding(
                    dimension="story",
                    severity=Severity.SUGGESTION,
                    issue="落版拍不在最后3镜:品牌 slogan/logo 需要片尾专用空间(§3 第 10.7 条)",
                    evidence=f"outro marks at {beat_marks.get('outro', [])}",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="把落版镜头移到末段 2-5s,保证旁白不争夺落版气口",
                ))

        # ── 抽象审核 · 相邻镜头机位雷同 ──────────────────────────────
        # e2e-f3e40f87 实证:S01-S05 camera 全是 static/dolly in 交替重复,
        # shot_size 逐镜不同所以 ST2 查不出——成片观感"每镜都差不多"。
        # 机位(camera)是镜头语言差异的直接载体:相邻镜 camera 逐字相同
        # (允许的例外:品牌落版末镜静态 logo,其 scene 含 logo/品牌/背景)。
        # 轮36:旧代码用压缩缓存(跳过空 camera)的下标去索引原 shots——
        # camera=[static,"",static] 时把"雷同"finding 挂到**没有 camera
        # 字段**的中间镜上(修订计划让 LLM 改不存在的字段,诱发 STALL),
        # 真实相邻对也可能被错位比较。改为显式 (shot,camera) 成对遍历。
        _cams = [(s, str(s.get("camera") or "").strip().lower())
                 for s in shots]
        _cams = [(s, c) for s, c in _cams if c]
        for (prev, pc), (cur, cc) in zip(_cams, _cams[1:]):
            # 落版专用镜头(纯色背景+logo/slogan)允许 static,不判机位重复
            cscene = str(cur.get("scene") or "")
            if pc == cc and not \
                    any(mark in cscene for mark in ("logo", "slogan", "落版", "背景")):
                cls = self.classifier.classify(
                    "storyboard", "camera_duplicated",
                    f"{prev.get('shot_id', '?')}->{cur.get('shot_id', '?')}")
                findings.append(Finding(
                    dimension="variation",
                    severity=Severity.CRITICAL,
                    # 轮41:点名实际比对对象——空 camera 的镜不参与配对,
                    # [static,'',static] 时被比较的是 S01/S03,"与上一镜"
                    # 的措辞在隔空镜时不严谨(六审残留)
                    issue=f"镜头{cur.get('shot_id', '?')}与镜头{prev.get('shot_id', '?')}同为机位'{cc}'——镜头语言无差异",
                    evidence=f"consecutive camera='{cc}' (S{prev.get('shot_id', '?')} -> S{cur.get('shot_id', '?')})",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="相邻镜头换用不同机位(dolly/truck/crane/pedestal 交替),景别也尽量跨档;品牌落版镜除外",
                ))

        # ── 抽象审核:旁白跨镜重复 + 占位符泄漏(剧本同款规则) ──────
        seen_narr: dict[str, list[str]] = {}
        for s in shots:
            n = s.get("narration")
            if isinstance(n, str) and n.strip():
                seen_narr.setdefault(_norm_narration_key(n), []).append(
                    str(s.get("shot_id") or "?"))
        for key, ids in seen_narr.items():
            if len(ids) >= 2:
                cls = self.classifier.classify(
                    "storyboard", "narration_duplicated", ids[0])
                findings.append(Finding(
                    dimension="language", severity=Severity.CRITICAL,
                    issue=f"旁白「{key[:40]}」在镜头 {', '.join(ids)} 重复出现——同一句配音要念两遍",
                    evidence=f"shots with identical narration: {ids}",
                    failure_mode=cls["mode"], revision_strategy=cls["strategy"],
                    proposed_fix="保留一处,另一镜改写为语义不同的旁白",
                ))

        for s in shots:
            samples = {
                "narration": s.get("narration", ""),
                "subject": s.get("subject", ""),
                "motion": s.get("motion", ""),
                "scene": s.get("scene", ""),
            }
            for field, text in samples.items():
                if not isinstance(text, str):
                    continue
                for pat in ("XX", "占位", "placeholder", "TBD", "TODO"):
                    if pat in text:
                        cls = self.classifier.classify(
                            "storyboard", "placeholder_leak", pat)
                        findings.append(Finding(
                            dimension="language", severity=Severity.CRITICAL,
                            issue=f"镜头{s.get('shot_id', '?')}的{field}含占位符'{pat}'",
                            evidence=f"{field}: {text[:60]}",
                            failure_mode=cls["mode"],
                            revision_strategy=cls["strategy"],
                            proposed_fix="替换为具体视觉描述/真实品牌名,禁止模板占位符",
                        ))

        return findings

    @staticmethod
    def _canonical_shot_size(value: Optional[str]) -> Optional[str]:
        """Normalize a shot-size string to a canonical bucket.

        'ECU'/'CU'/'MCU'/'MS'/'CS'/'WS'/'OW/OWS'/'Cowboy'/'Medium'/'Wide' - or None.
        """
        if not value:
            return None
        v = str(value).strip().lower().replace(" ", "")
        mapping = {
            "ecu": "ecu", "extremecloseup": "ecu", "超特写": "ecu", "大特写": "ecu",
            "cu": "cu", "close-up": "cu", "closeup": "cu", "close": "cu", "特写": "cu",
            "mcu": "mcu", "mediumclose-up": "mcu", "mediumcloseup": "mcu", "中近景": "mcu",
            "ms": "ms", "mediumshot": "ms", "medium": "ms", "中景": "ms", "中景镜头": "ms",
            "cs": "cs", "cowboy": "cs", "cowboyshot": "cs", "美式": "cs", "美式中景": "cs",
            "ws": "ws", "wideshot": "ws", "wide": "ws", "wide-lens": "ws", "全景": "ws",
            "ow": "ows", "ows": "ows", "extreme": "ows", "extreme-wide": "ows",
            "overtheshoulder": "ows", "大远景": "ows", "远景": "ows",
            "建立镜头": "ws", "establishing": "ws",
        }
        return mapping.get(v) or mapping.get(v.strip("-")) or None

    def _review_image_prompt(self, prompt_data: dict) -> list[Finding]:
        """Review image prompt quality."""
        findings = []
        entries = ([{"shot_id": "hero", "prompt_en": p.get("prompt_en", "")}
                    for p in prompt_data.get("hero_prompts", [])]
                   + list(prompt_data.get("shot_prompts", [])))
        for shot_prompt in entries:
            prompt = shot_prompt.get("prompt_en", "")
            shot_id = shot_prompt.get("shot_id", "?")

            # Check for subjective words/phrases (E1 map covers more cases)
            subjective = ["cinematic", "beautiful", "epic", "stunning", "amazing",
                          "confident smile", "warm atmosphere"]
            for word in subjective:
                if re.search(rf"\b{word}\b", prompt, re.IGNORECASE):
                    cls = self.classifier.classify("image_prompt", "subjective_word", word)
                    findings.append(Finding(
                        dimension="style",
                        severity=Severity.SUGGESTION,
                        issue=f"镜头{shot_id}含主观词'{word}'",
                        evidence=prompt[:200],
                        failure_mode=cls["mode"],
                        revision_strategy=cls["strategy"],
                        proposed_fix=f"删除或替换为视觉描述",
                    ))

            # Check style anchor at end
            style_anchor = prompt_data.get("style_anchor", "")
            if style_anchor and style_anchor not in prompt:
                cls = self.classifier.classify("image_prompt", "style_anchor_missing", "")
                findings.append(Finding(
                    dimension="style",
                    severity=Severity.CRITICAL,
                    issue=f"镜头{shot_id}缺少风格锚定词",
                    evidence=f"style_anchor='{style_anchor[:50]}...' not in prompt",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="在提示词末尾添加风格锚定词",
                ))

        return findings

    @staticmethod
    def _field(shot: dict, name: str) -> Optional[str]:
        """Read a shot field tolerating both `X` and `X_en` key styles."""
        for key in (f"{name}_en", name):
            v = shot.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return None

    # Subject-appearance phrases that violate the I2V principle (re-describing
    # what the reference image already shows). Scene/location phrases like
    # "in a coffee shop" are fine and must NOT match.
    _APPEARANCE_RE = re.compile(
        r"\b(?:wearing|dressed in|"
        r"(?:has|with) (?:long|short|brown|black|blonde|dark|curly|straight) hair|"
        r"(?:brown|blue|green|hazel|dark) eyes|"
        r"(?:he|she|they) (?:is|are) (?:a|an) \d+[- ]?(?:year[- ])?old|"
        r"in their (?:teens|twenties|thirties|forties))\b",
        re.IGNORECASE,
    )

    # Motion verbs (EN + CN) - a video prompt with none of these lacks motion.
    _MOTION_RE = re.compile(
        r"\b(?:walk|walks|walking|run|runs|running|turn|turns|turning|reach|reaches|"
        r"lift|lifts|push|pushes|pull|pulls|smile|smiles|smiling|nod|nods|nodding|"
        r"look|looks|glance|glances|step|steps|lean|leans|grab|grabs|throw|throws|"
        r"jump|jumps|sit|sits|stand|stands|rises|fall|falls|open|opens|close|closes|"
        r"wave|waves|point|points|whisper|whispers|speak|speaks|talk|talks|breathe|"
        r"blink|blinks|grin|grins|frown|frowns|cry|cries|laugh|laughs|drift|drifts|"
        r"flow|flows|swing|swings|spin|spins|dolly|pan|pans|truck|crane|tilt|tilts|"
        r"zoom|orbit|orbits|handheld|tracking|push(?:es)? in|pull(?:s)? out|"
        r"slowly|quickly|gently|suddenly|gradually)\b|"
        r"走过|跑向|转身|伸手|抬起|低头|微笑|点头|看向|举起|放下|推开|拉动|眨眼|呼吸|说|喊|笑|哭|"
        r"移动|靠近|远离|缓缓|快速|轻轻|突然|逐渐|摇摆|飘动|飞过|跳起|坐下|站起|"
        r"揉眼|停步|注水|冲泡|淡入|淡出|浮现|端起|捧起|捧杯|凝视|环视|推近|拉远|环绕|跟拍|"
        r"起身|步入|驻足|亮起|展开|翻开|喝|饮|尝|闻|拿起|摇头|睁眼|眯眼|舒展|落座|行走|迈步|"
        r"独行|握|搅|倒|注入|滴落|升腾|弥漫|飘散|滚动|流淌",
        re.IGNORECASE,
    )

    def _review_video_prompt(self, prompt_data: dict) -> list[Finding]:
        """Review video prompt quality."""
        findings = []
        for shot_prompt in prompt_data.get("shot_prompts", []):
            prompt = shot_prompt.get("prompt_text", "")
            shot_id = shot_prompt.get("shot_id", "?")

            # I2V check: subject-appearance descriptions repeated from the image
            matches = sorted({m.group(0).lower() for m in self._APPEARANCE_RE.finditer(prompt)})
            if matches:
                cls = self.classifier.classify("video_prompt", "i2v_appearance_repeat",
                                               ", ".join(matches))
                findings.append(Finding(
                    dimension="i2v",
                    severity=Severity.CRITICAL,
                    issue=f"镜头{shot_id}违反I2V原则:重复外观描述",
                    evidence=f"包含外观描述: {', '.join(matches)}",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="删除外观描述,只保留运动描述",
                ))

# Motion check: prompt should describe movement
            if prompt.strip() and not self._MOTION_RE.search(prompt):
                cls = self.classifier.classify("video_prompt", "i2v_motion_missing", shot_id)
                findings.append(Finding(
                    dimension="i2v",
                    severity=Severity.CRITICAL,
                    issue=f"镜头{shot_id}缺少运动描述",
                    evidence="提示词中没有动作/运动动词",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix="添加具体的动作动词+速度+幅度",
                ))

            # Camera term error (H1): wrong terms per the strategy corrections map
            wrong_terms = self.revision.STRATEGY_TEMPLATES.get("H1", {}).get("corrections", {})
            for wrong, right in wrong_terms.items():
                if re.search(rf"\b{re.escape(wrong)}\b", prompt, re.IGNORECASE):
                    cls = self.classifier.classify(
                        "video_prompt", "camera_term_error", wrong)
                    findings.append(Finding(
                        dimension="camera",
                        severity=Severity.CRITICAL,
                        issue=f"镜头{shot_id}镜头术语错误:'{wrong}'",
                        evidence=f"改用 '{right}'(视频生成模型不认 zoom/pan-head 术语)",
                        failure_mode=cls["mode"],
                        revision_strategy=cls["strategy"],
                        proposed_fix=f"把 '{wrong}' 替换为 '{right}'",
                    ))

            # Word count exceeded (I2): prompts beyond 380 chars lose the model
            if len(prompt) > self.VIDEO_PROMPT_MAX_CHARS:
                cls = self.classifier.classify("video_prompt", "word_count_exceeded", str(len(prompt)))
                findings.append(Finding(
                    dimension="format",
                    severity=Severity.CRITICAL,
                    issue=f"镜头{shot_id}提示词{len(prompt)}字,超过380字上限",
                    evidence="prompt_text length>380",
                    failure_mode=cls["mode"],
                    revision_strategy=cls["strategy"],
                    proposed_fix=f"精简到380字以内(删冗余状语,保留动作/镜头/光线)",
                ))

        return findings

    def _decide(self, stats: dict, round_num: int, improvement: Optional[float],
                max_rounds: int = 3, fix_applied: bool = False) -> Decision:
        """Determine action based on stats and improvement."""
        if stats["critical"] == 0:
            if stats["suggestion"] <= 3:
                return Decision.PASS
            else:
                return Decision.PASS_WITH_WARNINGS
        elif round_num >= max_rounds:
            return Decision.STOP
        elif improvement is not None and improvement <= 0 and not fix_applied:
            return Decision.STALL
        else:
            return Decision.REVISE

    def _next_action(self, decision: Decision, stage: str, round_num: int) -> str:
        """Get next action based on decision."""
        actions = {
            Decision.PASS: f"进入下一阶段({stage}审查通过)",
            Decision.PASS_WITH_WARNINGS: "记录警告,进入下一阶段",
            Decision.REVISE: "按 revision_plan 逐条修改后重新提交(未列字段不得改动)",
            Decision.STALL: f"迭代停滞,需要人工介入",
            Decision.STOP: f"达到最大轮次,人工介入",
        }
        return actions.get(decision, "未知")
