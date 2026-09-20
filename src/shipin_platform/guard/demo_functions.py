"""examples/platform_functions.py — 示例平台函数（模拟实现，可直接替换为真实 API）。

动作函数按六步流水线注册；审查函数体现两维度审查：
  A. 素材本身质量（artifact_quality）
  B. 放进成片里是否合规（in_video_compliance）——用户最看重的维度
"""
from __future__ import annotations

from typing import Any, Dict, List

from shipin_platform.guard import Registry


# ═══════════════════════ 第 1 步：需求澄清与粗剧本 ═══════════════════════

def _up(ctx: Dict[str, Any], step_name: str, fn_name: str) -> Dict[str, Any]:
    """从数据流取上游某函数的产物；取不到返回空 dict。"""
    return ((ctx or {}).get("steps", {}).get(step_name, {}) or {}).get(fn_name, {}) or {}


def ingest_user_input(raw_input: str = "", material_files: "list | None" = None,
                      idea: str = "") -> Dict[str, Any]:
    """进入第 1 步时自动执行：把用户输入归一为标准 intent。"""
    return {
        "raw_input": raw_input,
        "materials": material_files or [],
        "idea": idea,
        "injected": True,
    }


def clarify_intent(intent: str = "") -> Dict[str, Any]:
    """需求澄清：从用户输入提取结构化需求字段（模拟 LLM 提问+补全）。"""
    text = intent or "未提供输入"
    known_platform = "抖音" if "抖音" in text or "douyin" in text.lower() else "抖音"
    duration = 30
    if "15" in text:
        duration = 15
    elif "60" in text:
        duration = 60
    return {
        "content_type": "tvc",
        "platform": known_platform,
        "duration_sec": duration,
        "tone": "温暖治愈" if "温暖" in text or "治愈" in text else "干净现代",
        "audience": "都市白领",
        "brand": ("XX咖啡" if "咖啡" in text else "品牌方"),
        "constraints": ["无真人出镜", "必须出现品牌 Logo"] if "Logo" in text else ["无真人出镜"],
    }


def draft_rough_script(ctx: Dict[str, Any] = None, **_kw) -> Dict[str, Any]:
    """粗略剧本：三幕结构（钩子/转折/收尾）。 clarified 从数据流自动注入。"""
    c = _up(ctx, "intake_script", "clarify_intent") or {}
    d = int(c.get("duration_sec", 30))
    return {
        "topic": f"{c.get('brand', '品牌方')} {d}s {c.get('content_type', 'tvc')}",
        "brand": c.get("brand", "品牌方"),
        "duration_sec": d,
        "platform": c.get("platform", "抖音"),
        "structure": [
            {"beat": "hook",   "desc": "清晨，咖啡香唤醒城市", "sec": d // 6},
            {"beat": "turn",   "desc": "主角忙碌间隙，一杯咖啡的停顿", "sec": d // 3},
            {"beat": "resolve", "desc": "落版：品牌 Logo + slogan", "sec": d // 6},
        ],
    }


# ════════════════════════════ 第 2 步：剧本细化 ════════════════════════════

def refine_script(ctx: Dict[str, Any] = None, **_kw) -> Dict[str, Any]:
    """细剧本：逐场景旁白、时长（按粗剧本目标时长配比分配）、情绪曲线。"""
    r = _up(ctx, "intake_script", "draft_rough_script")
    d = int(r.get("duration_sec", 30))
    brand = r.get("brand", "品牌方")
    weights = [4, 6, 7, 7, 6]  # 占比：hook 短、value 长
    secs = [max(2, round(w * d / sum(weights))) for w in weights]
    secs[-1] += d - sum(secs)
    texts = ["清晨第一缕光，咖啡先醒", "城市在奔跑，她在等待", "一次停顿，一杯温度",
             "香气不慌张，好味不将就", f"{brand}，享受每一刻"]
    beats = ["hook", "build", "turn", "value", "outro"]
    scenes = [{"scene_id": f"S{i+1:02d}", "beat": beats[i],
               "narration": texts[i], "sec": secs[i]} for i in range(5)]
    return {
        "brand": brand,
        "platform": r.get("platform", "抖音"),
        "aspect": "9:16",
        "scenes": scenes,
        "total_duration_sec": sum(s["sec"] for s in scenes) or d,
        "emotion_curve": ["平静", "期待", "停顿", "满足", "回味"],
    }


# ════════════════════════════ 第 3 步：分镜制作 ════════════════════════════

def make_storyboard(ctx: Dict[str, Any] = None, **_kw) -> Dict[str, Any]:
    """剧本 → 分镜：每镜五要素 + 首尾动作 + 衔接。 script 从数据流自动注入。"""
    s = _up(ctx, "script_refine", "refine_script")
    scenes = s.get("scenes") or []
    sizes = ["ws", "ms", "cu", "mcu", "ws"]
    shots = []
    for i, sc in enumerate(scenes):
        shots.append({
            "shot_id": f"K{i+1:02d}",
            "scene_id": sc.get("scene_id", f"S{i+1:02d}"),
            "shot_size": sizes[i % len(sizes)],
            "camera": "dolly in" if i % 2 == 0 else "static",
            "motion": ("主角伸手握杯" if i in (2, 3) else "咖啡液面微动"),
            "scene_desc": sc.get("narration", "")[:12],
            "duration_sec": sc.get("sec", 4),
            "cause": "上一镜动作完成" if i > 0 else "开场",
            "effect": "动作延续到下一镜" if i < len(scenes) - 1 else "落版",
        })
    return {
        "aspect": s.get("aspect", "9:16"),
        "shots": shots,
        "total_duration_sec": sum(sh["duration_sec"] for sh in shots),
    }


# ═══════════════════════════ 第 4 步：首尾帧生成 ═══════════════════════════

def write_frame_prompts(ctx: Dict[str, Any] = None, **_kw) -> Dict[str, Any]:
    """首尾帧提示词：主体锚定逐字一致。 storyboard 从数据流自动注入。"""
    sb = _up(ctx, "storyboard", "make_storyboard")
    prompts = []
    for sh in sb.get("shots", []):
        base = (f"主角：25岁左右年轻女性，黑色长直发披肩、米白色针织开衫。"
                f"{sh.get('motion', '动作')}，{sh.get('shot_size', 'ms')} 景别")
        prompts.append({
            "shot_id": sh["shot_id"],
            "first_prompt": base + "，动作起始状态",
            "last_prompt": base + "，动作完成状态",
        })
    return {"prompts": prompts}


def generate_keyframes(ctx: Dict[str, Any] = None, resolution: str = "720x1280",
                       subject_drift: float = 0.0,
                       **_kw) -> Dict[str, Any]:
    """生成首尾帧（模拟，竖屏 720x1280）。subject_drift 模拟主体漂移：0=完全一致。
    首尾帧提示词从数据流自动注入（同步骤上一次调用的产物）。"""
    fp = _up(ctx, "keyframes", "write_frame_prompts")
    pr = fp.get("prompts", [])
    w, h = (int(x) for x in resolution.lower().split("x"))
    drift = max(0.0, min(1.0, subject_drift))
    frames = []
    for p in pr:
        frames.append({
            "shot_id": p["shot_id"],
            "first": f"{p['shot_id']}_first.png",
            "last": f"{p['shot_id']}_last.png",
            "width": w, "height": h,
        })
    anchor = "A1" if drift <= 0.15 else f"A1-drift{drift:.2f}"
    return {
        "frames": frames,
        "resolution": resolution,
        "aspect": "9:16" if h > w else "16:9",
        "subject_drift": drift,
        "anchor_ids": [anchor] * len(frames),
        "style_consistent": drift <= 0.3,
    }


# ═══════════════════════════ 第 5 步：视频生成 ═════════════════════════════

def write_video_prompts(ctx: Dict[str, Any] = None, **_kw) -> Dict[str, Any]:
    kf = _up(ctx, "keyframes", "generate_keyframes")
    return {"prompts": [
        {"shot_id": f["shot_id"],
         "video_prompt": f"单镜头连续动作，{f['shot_id']} 首帧推至末帧",
         "first_frame": f["first"], "last_frame": f["last"]}
        for f in kf.get("frames", [])]}


def generate_video(ctx: Dict[str, Any] = None, fps: int = 24,
                   motion_consistency: float = 1.0, internal_cuts: int = 0,
                   **_kw) -> Dict[str, Any]:
    """生成视频段（模拟）。motion_consistency<0.7 模拟运动断裂；internal_cuts>0 模拟镜头内切。
    视频提示词与分镜时长从数据流自动注入。"""
    vp = _up(ctx, "video_gen", "write_video_prompts")
    sb = _up(ctx, "storyboard", "make_storyboard")
    dur_by_shot = {s["shot_id"]: s.get("duration_sec", 4) for s in sb.get("shots", [])}
    mc = max(0.0, min(1.0, motion_consistency))
    clips = []
    for p in vp.get("prompts", []):
        clips.append({
            "shot_id": p["shot_id"],
            "clip": f"{p['shot_id']}_clip.mp4",
            "fps": fps,
            "duration_sec": float(dur_by_shot.get(p["shot_id"], 4.0)),
            "first_frame_ref": p["first_frame"],
            "last_frame_ref": p["last_frame"],
        })
    return {
        "clips": clips,
        "fps": fps,
        "motion_consistency": mc,
        "internal_cuts": internal_cuts,
        "total_duration_sec": sum(c["duration_sec"] for c in clips),
    }


# ═══════════════════════════ 第 6 步：后期制作 ═════════════════════════════

def edit_timeline(ctx: Dict[str, Any] = None, **_kw) -> Dict[str, Any]:
    vc = _up(ctx, "video_gen", "generate_video")
    clips = vc.get("clips", [])
    return {"timeline": [c["shot_id"] for c in clips],
            "transitions": ["dissolve"] * max(0, len(clips) - 1),
            "total_duration_sec": sum(c["duration_sec"] for c in clips)}


def add_voiceover(ctx: Dict[str, Any] = None, **_kw) -> Dict[str, Any]:
    tl = _up(ctx, "post_production", "edit_timeline")
    sc = _up(ctx, "script_refine", "refine_script")
    return {"voice": "biz_female", "segments": len((tl or {}).get("timeline", [])),
            "script_source": bool(sc)}


def add_music(music: str = "warm_60s.wav", duck: bool = True, **_kw) -> Dict[str, Any]:
    return {"bgm": music, "ducked": duck, "bgm_gain_db": -19.0}


def add_effects(style: str = "warm", vignette: bool = True, **_kw) -> Dict[str, Any]:
    return {"color_grade": style, "vignette": vignette}


def render_final(ctx: Dict[str, Any] = None, **_kw) -> Dict[str, Any]:
    fx = _up(ctx, "post_production", "add_effects")
    vc = _up(ctx, "video_gen", "generate_video")
    return {"final": "final.mp4", "duration_sec": vc.get("total_duration_sec", 0),
            "lufs": -14.0,
            "has_subtitles": True, "black_frames": 0,
            "color": fx.get("color_grade", "warm")}


# ═══════════════════════════════ 审查函数 ══════════════════════════════════
# 审查两维度：artifact_quality（素材本身） / in_video_compliance（放进成片是否合规）

def _rule(rule_id: str, dim: str, passed: bool, detail: str) -> Dict[str, Any]:
    return {"rule": rule_id, "dimension": dim, "passed": passed, "detail": detail}


def review_rough_script(artifacts: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    rules = []
    intent = artifacts.get("ingest_user_input", {})
    clarified = artifacts.get("clarify_intent", {})
    rough = artifacts.get("draft_rough_script", {})
    # A. 素材本身
    rules.append(_rule("requirements.clarified", "artifact_quality",
                       bool(clarified.get("duration_sec")), 
                       f"需求字段完整性：duration_sec={'√' if clarified.get('duration_sec') else '×'}"))
    rules.append(_rule("rough.script_structure", "artifact_quality",
                       len(rough.get("structure", [])) >= 3 and
                       rough.get("structure", [{}])[0].get("beat") == "hook",
                       "粗剧本三幕结构完整且以 hook 开场"))
    # B. 成片合规
    rules.append(_rule("rough.platform_declared", "in_video_compliance",
                       rough.get("platform") in ("抖音", "B站", "微信", "快手"),
                       f"目标平台已声明：{rough.get('platform')}"))
    rules.append(_rule("rough.duration_plausible", "in_video_compliance",
                       10 <= int(rough.get("duration_sec", 0)) <= 300,
                       f"成片时长在合理区间：{rough.get('duration_sec')}s"))
    return _verdict(rules, context, "需求澄清+粗剧本")


def review_script(artifacts: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    rules = []
    script = artifacts.get("refine_script", {})
    scenes = script.get("scenes", [])
    total = script.get("total_duration_sec", 0)
    narr_chars = sum(len(s.get("narration", "")) for s in scenes)
    target = (((context.get("previous_artifacts") or {}).get("intake_script") or {})
              .get("draft_rough_script") or {}).get("duration_sec", total)
    platform = script.get("platform", "")
    # A. 素材本身
    rules.append(_rule("script.scenes_complete", "artifact_quality",
                       len(scenes) >= 4 and all(s.get("narration") for s in scenes),
                       f"场景数 {len(scenes)}，全部含旁白"))
    rules.append(_rule("script.emotion_curve", "artifact_quality",
                       len(script.get("emotion_curve", [])) == len(scenes),
                       "情绪曲线与场景一一对应"))
    # B. 成片合规
    rules.append(_rule("script.duration_match", "in_video_compliance",
                       abs(total - int(target)) <= 3,
                       f"总时长 {total}s 与粗剧本目标 {target}s 匹配（±3s）"))
    rules.append(_rule("script.narration_density", "in_video_compliance",
                       narr_chars <= total * 4.5,
                       f"旁白密度 {narr_chars}/{total}s 字符，不超播读极限"))
    want_aspect = "9:16" if platform == "抖音" else "16:9"
    rules.append(_rule("script.platform_aspect", "in_video_compliance",
                       script.get("aspect") == want_aspect,
                       f"{platform or '平台'} 要求画幅 {want_aspect}，实际 {script.get('aspect')}"))
    return _verdict(rules, context, "剧本细化")


def review_storyboard(artifacts: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    rules = []
    sb = artifacts.get("make_storyboard", {})
    shots = sb.get("shots", [])
    want_aspect = ((((context.get("previous_artifacts") or {}).get("script_refine") or {})
                    .get("refine_script") or {}).get("aspect")) or "9:16"
    # A. 素材本身
    rules.append(_rule("storyboard.shot_count", "artifact_quality",
                       4 <= len(shots) <= 12, f"镜头数 {len(shots)}"))
    rules.append(_rule("storyboard.fields_complete", "artifact_quality",
                       all(all(k in sh for k in
                               ("shot_size", "camera", "motion", "duration_sec"))
                           for sh in shots),
                       "每镜含景别/机位/动作/时长四要素"))
    # B. 成片合规
    adj_ok = all(shots[i]["shot_size"] != shots[i + 1]["shot_size"]
                 for i in range(len(shots) - 1)) if shots else False
    rules.append(_rule("storyboard.adjacent_shot_size", "in_video_compliance",
                       adj_ok, "相邻镜头景别不同（避免跳剪感）"))
    rules.append(_rule("storyboard.chain_cause_effect", "in_video_compliance",
                       all(sh.get("cause") and sh.get("effect") for sh in shots),
                       "每镜有承接与衔接（保证成片连贯）"))
    rules.append(_rule("storyboard.aspect_match", "in_video_compliance",
                       sb.get("aspect") == want_aspect,
                       f"画幅 {sb.get('aspect')} 与剧本目标 {want_aspect} 一致"))
    return _verdict(rules, context, "分镜制作")


def review_keyframes(artifacts: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    rules = []
    kf = artifacts.get("generate_keyframes", {})
    frames = kf.get("frames", [])
    want_aspect = ((((context.get("previous_artifacts") or {}).get("storyboard") or {})
                    .get("make_storyboard") or {}).get("aspect")) or "9:16"
    # A. 素材本身质量
    rules.append(_rule("keyframes.resolution_min", "artifact_quality",
                       bool(frames) and all(min(f["width"], f["height"]) >= 720 for f in frames),
                       f"分辨率 {kf.get('resolution')}，短边需 ≥ 720"))
    rules.append(_rule("keyframes.subject_complete", "artifact_quality",
                       kf.get("subject_drift", 1.0) <= 0.15,
                       f"主体完整度：漂移 {kf.get('subject_drift')} ≤ 0.15"))
    # B. 放进成片里是否合规（重点）
    rules.append(_rule("keyframes.aspect_matches_video", "in_video_compliance",
                       kf.get("aspect") == want_aspect,
                       f"画幅 {kf.get('aspect')} 与目标成片 {want_aspect} 一致"))
    rules.append(_rule("keyframes.anchor_consistency", "in_video_compliance",
                       len(set(kf.get("anchor_ids", []))) == 1,
                       "跨镜头主体锚定一致（成片中人物不跳变）"))
    rules.append(_rule("keyframes.style_consistent", "in_video_compliance",
                       kf.get("style_consistent", False),
                       "全片风格统一（成片不出现风格断层）"))
    return _verdict(rules, context, "首尾帧生成")


def review_video_clips(artifacts: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    rules = []
    vc = artifacts.get("generate_video", {})
    clips = vc.get("clips", [])
    # A. 素材本身
    rules.append(_rule("video.fps_match", "artifact_quality",
                       all(c["fps"] == 24 for c in clips),
                       f"全片 {vc.get('fps')}fps 统一"))
    rules.append(_rule("video.no_internal_cuts", "artifact_quality",
                       vc.get("internal_cuts", 99) == 0,
                       f"无镜头内切（{vc.get('internal_cuts')} 处）"))
    # B. 放进成片里是否合规（重点）
    rules.append(_rule("video.motion_continuity", "in_video_compliance",
                       vc.get("motion_consistency", 0) >= 0.7,
                       f"运动连续性 {vc.get('motion_consistency')} ≥ 0.7（成片动作衔接）"))
    rules.append(_rule("video.frames_chain", "in_video_compliance",
                       all(c.get("first_frame_ref") and c.get("last_frame_ref")
                           for c in clips),
                       "首尾帧引用完整（成片边界可拼接）"))
    script_total = float(((((context.get("previous_artifacts") or {})
                            .get("script_refine") or {}).get("refine_script")
                           or {}).get("total_duration_sec") or 0))
    rules.append(_rule("video.total_duration", "in_video_compliance",
                       abs(float(vc.get("total_duration_sec", 0)) - script_total) <= 3,
                       f"总时长 {vc.get('total_duration_sec')}s 与剧本目标匹配（±3s）"))
    return _verdict(rules, context, "视频生成")


def review_final_film(artifacts: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    rules = []
    final = artifacts.get("render_final", {})
    # A. 素材本身
    rules.append(_rule("final.no_black_frames", "artifact_quality",
                       final.get("black_frames", 99) == 0, "无黑帧"))
    rules.append(_rule("final.has_subtitles", "artifact_quality",
                       final.get("has_subtitles", False), "字幕已烧录"))
    # B. 放进成片里是否合规（重点）
    rules.append(_rule("final.lufs_broadcast", "in_video_compliance",
                       -16 <= final.get("lufs", 0) <= -12,
                       f"响度 {final.get('lufs')} LUFS 达标（-16~-12）"))
    video_total = float((((context.get("previous_artifacts") or {}).get("video_gen")
                          or {}).get("generate_video") or {}).get("total_duration_sec", 0))
    rules.append(_rule("final.duration_match", "in_video_compliance",
                       abs(final.get("duration_sec", 0) - video_total) <= 3,
                       f"成片时长 {final.get('duration_sec')}s 与视频段总长 {video_total}s 匹配"))
    rules.append(_rule("final.brand_elements", "in_video_compliance",
                       final.get("color") == "warm",
                       "品牌氛围元素齐全（暖色调落版）"))
    return _verdict(rules, context, "后期制作")


def _verdict(rules: List[Dict[str, Any]], context: Dict[str, Any], label: str) -> Dict[str, Any]:
    passed = all(r["passed"] for r in rules)
    failed = [f"{r['rule']}({r['dimension']}): {r['detail']}" for r in rules if not r["passed"]]
    return {
        "verdict": "pass" if passed else "fail",
        "rules": rules,
        "summary": (f"「{label}」审查通过（{len(rules)} 条规则全绿）"
                    if passed else
                    f"「{label}」审查未通过，{len(failed)} 条不达标：{'；'.join(failed)}"),
    }


# ── 注册表装配 ──────────────────────────────────────────────
def build_registry() -> Registry:
    reg = Registry()
    reg.register("ingest_user_input", ingest_user_input,
                 description="进入第1步自动执行：归一用户输入")
    reg.register("clarify_intent", clarify_intent,
                 description="需求澄清：提取平台/时长/基调等结构化字段",
                 params_hint="intent: str")
    reg.register("draft_rough_script", draft_rough_script,
                 description="产出三幕结构粗剧本", params_hint="clarified: dict")
    reg.register("refine_script", refine_script,
                 description="剧本细化：逐场景旁白/时长/情绪", params_hint="rough_script: dict")
    reg.register("make_storyboard", make_storyboard,
                 description="剧本→分镜（五要素+衔接）", params_hint="script: dict")
    reg.register("write_frame_prompts", write_frame_prompts,
                 description="首尾帧提示词（主体锚定）", params_hint="storyboard: dict")
    reg.register("generate_keyframes", generate_keyframes,
                 description="生成首尾帧图片（可注错：resolution/subject_drift）",
                 params_hint="frame_prompts, resolution, subject_drift")
    reg.register("write_video_prompts", write_video_prompts,
                 description="视频提示词", params_hint="keyframes: dict")
    reg.register("generate_video", generate_video,
                 description="生成视频段（可注错：motion_consistency/internal_cuts/fps）",
                 params_hint="video_prompts, fps, motion_consistency, internal_cuts")
    reg.register("edit_timeline", edit_timeline, description="剪辑时间线")
    reg.register("add_voiceover", add_voiceover, description="配音")
    reg.register("add_music", add_music, description="音乐+闪避")
    reg.register("add_effects", add_effects, description="特效/调色")
    reg.register("render_final", render_final, description="渲染成片")
    reg.register_reviewer("review_rough_script", review_rough_script)
    reg.register_reviewer("review_script", review_script)
    reg.register_reviewer("review_storyboard", review_storyboard)
    reg.register_reviewer("review_keyframes", review_keyframes)
    reg.register_reviewer("review_video_clips", review_video_clips)
    reg.register_reviewer("review_final_film", review_final_film)
    return reg
