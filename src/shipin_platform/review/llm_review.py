"""LLM semantic review with fixed rubrics — server-side, auditable.

规则引擎（engine.py）只查结构：字段齐不齐、字数超没超。剧情逻辑（钩子
是否成立、镜头是否可拍、镜间过渡是否自然）规则查不出来，这正是「中间
五道关全 PASS、问题全漏到成片」的另一半根因。本模块把语义审查固化成
服务端 rubric：同一输入同一 rubric，输出严格 JSON，findings 与规则引擎
合并出 decision。无 key 时 available=false，调用方自行降级。
"""
from __future__ import annotations

import json
import os
import re
import socket
import ipaddress
from pathlib import Path
from typing import Optional

import requests

# ZCode: honor AGNES_BASE_URL (.env.example documents it, but this module
# hardcoded the URL so a relay/proxy endpoint could never be used).
_AGNES_BASE = os.environ.get(
    "AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1").rstrip("/")
CHAT_URL = f"{_AGNES_BASE}/chat/completions"
ALLOWED_HOST = {"apihub.agnes-ai.com"}
try:
    import urllib.parse as _urlparse
    _h = _urlparse.urlparse(_AGNES_BASE).hostname
    if _h:
        ALLOWED_HOST.add(_h)
except Exception:
    pass

SCRIPT_RUBRIC = """你是严格的 TVC 审片人。依据文本事实（不要臆造画面）按以下维度评审脚本：
1. story_structure：钩子（前3秒有反常细节/悬念）、痛点或冲突、转折（产品介入）、收束落版，四拍是否齐全且递进；
2. narration_pacing：每句旁白 ≤14 字、口语化、无"然后"式流水账、总字数与时长匹配（约 2.7 字/秒）；
3. feasibility：每个镜头必须是文生视频模型单镜头可连续拍出的画面——一个场景、一个连续动作；凡需要"多景切换/多事件"的镜头都判 critical；
4. continuity：相邻镜头之间有可拍的自然过渡（人物移动/视线/光线/动作接力），否则判 critical；
5. brand_integration：品牌元素贯穿（≥3 镜）且末镜是品牌落版。

输出严格 JSON（不要 markdown 代码块、不要解释）：
{"scores": {"story_structure": 0-100, "narration_pacing": 0-100, "feasibility": 0-100, "continuity": 0-100, "brand_integration": 0-100},
 "findings": [{"dimension": "维度名", "severity": "critical"或"suggestion", "issue": "问题", "evidence": "引用脚本原文", "fix": "怎么改"}]}
severity=critical 表示不修改就禁止进入下一阶段。没有问题则 findings 为空数组。"""

STORYBOARD_RUBRIC = """你是严格的 TVC 分镜审片人。依据分镜文本按以下维度评审：
1. single_action：每镜只包含一个连续动作（5 秒内无法完成多动作/换景/多事件），违反判 critical；
2. continuity：cause/effect 字段必须是可拍的视觉过渡（转身/推门/视线/光线/动作接力），"导致"式空洞因果判 critical；
3. identity：同一主角在各镜头的特征描述（衣着/发型/体貌）是否逐字一致，不一致判 critical；
4. shot_variation：相邻镜头景别/机位必须有差异；
5. brand：品牌元素 ≥3 镜且末镜为品牌落版。

输出严格 JSON（不要 markdown 代码块、不要解释）：
{"scores": {"single_action": 0-100, "continuity": 0-100, "identity": 0-100, "shot_variation": 0-100, "brand": 0-100},
 "findings": [{"dimension": "维度名", "severity": "critical"或"suggestion", "issue": "问题", "evidence": "引用分镜原文", "fix": "怎么改"}]}
severity=critical 表示不修改就禁止进入下一阶段。没有问题则 findings 为空数组。"""

CANVAS_TEXT_RUBRIC = """你是严格的短视频工作流审片人。以下是一段待评审的创作内容（可能是剧本、分镜、
提示词等不同阶段产物）。按以下通用维度评审：
1. clarity：内容是否完整表达核心意图，信息是否清楚无歧义；
2. feasibility：内容描述的镜头/画面是否可由一个文生视频单镜头连续生成（单场景单动作），
   需要多景切换/多事件/多步骤的都判 critical；
3. consistency：场景、主体、风格描述内部是否一致、与给定背景信息是否吻合；
4. production_quality：内容是否达到可投入生产的完整度（关键画面元素、时长、风格都有交代），
   还是空泛模糊仅剩骨架。

输出严格 JSON（不要 markdown 代码块、不要解释）：
{"scores": {"clarity": 0-100, "feasibility": 0-100, "consistency": 0-100, "quality": 0-100},
 "findings": [{"severity": "critical"或"suggestion", "issue": "问题", "evidence": "引用内容原文片段", "fix": "怎么改"}],
 "summary": "不超过 60 字的一句话评审结论"}
severity=critical 表示不修改就禁止进入下一阶段；没有问题则 findings 为空数组。"""


def llm_text_review(text: str, context: Optional[str] = None) -> dict:
    """画布 review 节点用的通用自动审查：文本 + 可选上下文 → 结构化结论。

    与 llm_stage_review（管线语义审查）互补：它是管线阶段专用 rubric，
    这里服务任意节点（提示词/分镜/脚本等）。verdict:
    pass=可由生产直接放行；reject=存在 critical 问题必须返工修改。
    """
    key = _llm_key()
    if not key:
        return {"available": False, "reason": "AGNES_KEY 未配置，跳过自动审查"}
    prompt = CANVAS_TEXT_RUBRIC
    if context:
        prompt += "\n\n额外背景：" + str(context)[:800]
    prompt += "\n\n待审内容：\n" + str(text)[:6000]
    prompt += ("\n\n再次强调：只输出一个 JSON 对象本身，不要 markdown 代码块，"
               "不要任何解释文字。")
    try:
        raw = _ask_llm(prompt, key)
    except Exception as e:
        return {"available": False,
                "reason": f"自动审查调用失败: {type(e).__name__}: {str(e)[:120]}"}
    parsed = _parse_json_object(raw)
    if parsed is None:
        return {"available": False, "reason": "自动审查输出无法解析为 JSON",
                "raw": raw[:500]}
    findings = []
    for f in (parsed.get("findings") or []):
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "suggestion").lower()
        findings.append({
            "severity": "critical" if sev == "critical" else "suggestion",
            "issue": str(f.get("issue") or "")[:300],
            "evidence": str(f.get("evidence") or "")[:300],
            "fix": str(f.get("fix") or "")[:300],
        })
    criticals = [f for f in findings if f["severity"] == "critical"]
    scores = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
    verdict = str(parsed.get("verdict") or "")
    if not verdict:
        # 未显式给 verdict 时按发现兜底：critical→reject，否则 pass
        verdict = "reject" if criticals else "pass"
    return {"available": True, "scores": scores, "findings": findings,
            "verdict": verdict,
            "summary": str(parsed.get("summary") or "")[:300],
            "raw": raw[:2000]}


def _parse_json_object(text: str) -> Optional[dict]:
    try:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None
    except (json.JSONDecodeError, AttributeError):
        return None


def _llm_key() -> str:
    key = os.environ.get("AGNES_KEY", "").strip()
    if _key_ok(key):
        return key
    tf = Path(os.environ.get("TEMP", "")) / "agnes_key.txt" if os.environ.get("TEMP") else None
    if tf and tf.exists():
        try:
            key = tf.read_text(encoding="utf-8").strip()
        except OSError:
            key = ""
        if _key_ok(key):
            return key
    return ""


def _key_ok(key: str) -> bool:
    """机器密钥形状校验(与 hard_gates 一致):仅可打印 ASCII 且 ≥16 字符——挡
    「模型返回文本被误当 key」进 Authorization 头(非 ASCII 会触发 http.client
    latin-1 UnicodeEncodeError,实测事故)。"""
    if not key or len(key) < 16:
        return False
    return all(32 < ord(c) < 127 for c in key)


def _check_ssrf(url: str) -> str:
    from urllib.parse import urlparse
    u = urlparse(url)
    # ZCode: an operator-configured relay endpoint (AGNES_BASE_URL, e.g. an
    # SSH reverse tunnel to the real API) may speak plain http; the default
    # upstream stays https-only with the IP checks below.
    if u.hostname in ALLOWED_HOST and u.scheme == "http":
        return url
    assert u.scheme == "https", "https only"
    assert u.hostname in ALLOWED_HOST, "host not in allowlist"
    for info in socket.getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0])
        assert not (ip.is_loopback or ip.is_link_local or ip.is_reserved), "blocked IP"
    return url


def _ask_llm(prompt: str, key: str, max_tokens: int = 2200) -> str:
    import os as _os
    body = {
        "model": _os.environ.get("SHIPIN_LLM_MODEL", "agnes-3.0-flash"),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    resp = requests.post(_check_ssrf(CHAT_URL), json=body,
                         headers={"Authorization": f"Bearer {key}",
                                  "Content-Type": "application/json"},
                         timeout=180)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def llm_stage_review(stage: str, data: dict, brief: Optional[dict] = None) -> dict:
    """Run the fixed-rubric semantic review for script / storyboard.

    Returns {"available": bool, "reason": str, "scores": {...},
    "findings": [...normalized...], "raw": str}.
    findings are normalized to {"dimension","severity","issue","evidence","fix"};
    severity anything other than critical/suggestion is downgraded to suggestion.
    """
    key = _llm_key()
    if not key:
        return {"available": False, "reason": "AGNES_KEY 未配置，跳过语义审查",
                "scores": {}, "findings": [], "raw": ""}

    if stage == "script":
        rubric = SCRIPT_RUBRIC
        payload = {"duration_sec": data.get("duration_sec"),
                   "shots": data.get("shots", [])}
        if brief:
            payload["brief"] = {k: brief.get(k) for k in
                                ("product_info", "target_platform", "tone",
                                 "duration_sec", "creative_direction") if brief.get(k)}
    elif stage == "storyboard":
        rubric = STORYBOARD_RUBRIC
        payload = {"shots": data.get("shots", data.get("scenes", []))}
    else:
        return {"available": False, "reason": f"stage {stage} 无语义审查 rubric",
                "scores": {}, "findings": [], "raw": ""}

    prompt = (rubric + "\n\n待审数据 JSON：\n"
              + json.dumps(payload, ensure_ascii=False)[:12000])

    def _parse(text: str):
        try:
            m = re.search(r"\{.*\}", text, re.S)
            return json.loads(m.group(0)) if m else None
        except (json.JSONDecodeError, AttributeError):
            return None

    try:
        raw = _ask_llm(prompt, key)
    except Exception as e:
        return {"available": False,
                "reason": f"语义审查调用失败: {type(e).__name__}: {str(e)[:120]}",
                "scores": {}, "findings": [], "raw": ""}
    parsed = _parse(raw)
    if parsed is None:
        # coffee-v5 教训：LLM 偶发输出非 JSON——收紧重试一次，别让审查静默失效
        try:
            raw = _ask_llm(prompt + "\n\n再次强调：只输出一个 JSON 对象本身，"
                           "不要 markdown 代码块，不要任何解释文字。", key)
            parsed = _parse(raw)
        except Exception as e:
            return {"available": False,
                    "reason": f"语义审查重试失败: {type(e).__name__}: {str(e)[:120]}",
                    "scores": {}, "findings": [], "raw": ""}
    if parsed is None:
        return {"available": False, "reason": "语义审查输出无法解析为 JSON",
                "scores": {}, "findings": [], "raw": raw[:500]}

    findings = []
    for f in (parsed.get("findings") or []):
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or "suggestion").lower()
        findings.append({
            "dimension": str(f.get("dimension") or "semantic")[:40],
            "severity": "critical" if sev == "critical" else "suggestion",
            "issue": str(f.get("issue") or "")[:300],
            "evidence": str(f.get("evidence") or "")[:300],
            "fix": str(f.get("fix") or "")[:300],
        })
    scores = parsed.get("scores") if isinstance(parsed.get("scores"), dict) else {}
    return {"available": True, "reason": "", "scores": scores,
            "findings": findings, "raw": raw[:2000]}
