"""Server-side pipeline runner - the ComfyUI-ification of the workflow.

用户痛点(coffee-v5/v6 复盘):编排权在 agent 手里时,SKILL 写得再细子
智能体也会走偏;首尾帧链不链、失败了怎么重试、转场用切还是叠化,这些
判断分散在文档里靠 agent 自觉。本模块把整个制作流程收进平台,agent 只剩
四个调用:

    POST /api/pipeline/text      → 服务端 LLM 生成剧本/分镜 + 审核循环
    POST /api/project/confirm    → 人工确认闸门(不变)
    POST /api/pipeline/generate  → 首帧图→锚定视频→QC→重试→TTS→对齐
    POST /api/pipeline/assemble  → 转场拼接→调色→字幕→声音设计→终验→发布

所有判断都是确定性代码;LLM 只在生成文案/语义审查两个点出现,输出必须
通过同一套规则引擎。

首尾帧链式策略(coffee-v6 机制化):
- 相邻两镜景别相差 ≤1 档 且 主场景词相同 → **链式**:last_frame = 下一镜
  首帧图,拼接边界用硬切(动作无缝接力);
- 否则 → **自末帧**:为本镜生成"动作完成态"末帧图,拼接边界用 dissolve。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from shipin_platform import roots
from typing import Optional

from shipin_platform.assembly import (
    align_narration, build_transition_stitch, master_audio, kenburns,
    mux_audio_video, normalize_loudness, color_grade_warm, burn_srt,
    loudness_measure, DEFAULT_TD,
)
from shipin_platform.review.engine import ReviewEngine
from shipin_platform.review.clip_qc import qc_clip
from shipin_platform.review.llm_review import llm_stage_review
from shipin_platform.generation.generate_assets import (
    generate_image_agnes, generate_video_agnes,
)
from shipin_platform.services.template_registry import (
    get_registry as get_template_registry,
)
from shipin_platform.services.component_registry import (
    get_registry as get_component_registry,
)
from shipin_platform.services.costing import record_cost

ROOT = roots.data_root()
PROJECTS_DIR = ROOT / "data" / "projects"
BGM_PATH = ROOT / "assets" / "bgm" / "warm_60s.wav"

SIZE_ORDER = ["ecu", "cu", "mcu", "cs", "ms", "ws", "ows"]
LOCATION_TOKENS = ["写字楼", "办公室", "会议室", "办公桌", "工位", "前台",
                   "电梯", "楼梯", "走廊", "过道", "门口", "门厅", "大堂",
                   "吧台", "餐桌", "厨房", "窗边", "店铺", "店内", "商场",
                   "广场", "街道", "街", "路口", "路边", "天台", "屋顶",
                   "车间", "仓库", "工地", "车内", "车上", "客厅", "卧室",
                   "餐厅", "咖啡", "洗手间", "阳台", "泳池", "球场", "舞台",
                   "候机", "月台", "落版"]

SCRIPT_PROMPT = """你是严格的 TVC 编剧。依据 brief 创作剧本,输出严格 JSON(无 markdown、无解释):
{"duration_sec": <int>, "shots": [{"shot_id": "S01", "duration_sec": <2-5>, "narration": "<一句≤14字的画外旁白,可空串>", "dialogue": {"role_code": "hero_male|colleague_male|assistant_female", "text": "<该角色在镜头内说出口的台词,≤20字>"} 或省略, "scene": "<单一地点单一事件,≤30字>"}]}
硬规则:镜头 5-10 个(短片取下限,保证每镜 ≥2.5s);结构 hook→pain→turn→value→outro(末镜=品牌落版);每镜必须有 narration 或 dialogue 之一(两拍换声,忌双轨同压);**全片至少 2 镜必须有 dialogue**(真人开口说话,禁全片只剩话外音旁白);总字数(旁白+台词 words) ≈ duration×2.7;台词必须口语短句、一句一个意思、≤20字,禁书面腔;台词与旁白不得重复同一句话(同一信息二选一);说话角色每镜至多一个,ROLE_CODE 只取自列表;禁"然后"与旁白覆盖产品价值句;每个 scene 只有一个地点一个事件(单镜头必须可连续拍完);品牌元素≥3镜。
台词质感红线:dialogue 必须像真人开口说话——口语短句(如"你喝一口试试""等我三分钟"),禁书面语长句、禁宣传口号腔;全片任意两句 narration 禁止逐字重复(同一句画外旁白全片只能出现一次);全片 narration/dialogue 禁止出现 XX/xxx/占位符/TBD/TODO/【】 等任何模板占位符,品牌名一律用 brief 中的真实名称;旁白念出来必须自然,有停顿有语气,禁诗歌腔。
落版镜专项:末镜必须是【静态可拍画面】--暖色调背景上产品静置,品牌名与 slogan 以字幕/台词呈现(不在 scene 里写"叠加/浮现/多景切换"等非拍摄描述);末镜 narration=品牌名+slogan(不再设 dialogue)。

brief:
{brief}"""

STORYBOARD_PROMPT = """你是严格的 TVC 分镜师。把剧本展开为分镜，输出严格 JSON（无 markdown、无解释）：
{"hero_shot": "<shot_id>", "shots": [{"shot_id": "...", "duration_sec": <秒>, "beat": "hook|pain|turn|value|outro", "rhythm": "slow|medium|fast", "sfx": "sfx_<n>", "shot_size": "ecu|cu|mcu|cs|ms|ws|ows", "subject": "<与全片逐字一致的主角锚定>", "motion": "<英文运动短语，必含具体动作动词+速度/幅度词，如 slowly lifts the cup / turns her head gently / steam rises softly，禁中文>", "scene": "<单一地点>", "spatial": "<构图>", "camera": "<机位/运动术语：dolly/truck/crane/pedestal，禁zoom表移动>", "cause": "<承接上一镜的可拍视觉过渡>", "effect": "<给下一镜的可拍衔接点>", "narration": "<逐字复制剧本旁白,无旁白则空串>", "dialogue": "<逐字复制剧本台词,无台词则空串>", "speaking": "<仅当有 dialogue 时:该角色说话时的面部与口型英文描述,如 speaks the line with lips moving clearly, mouth shapes visible; 无台词则为空串>"}]}
硬规则：每镜只一个连续动作；motion 必须是英文且含动作动词与速度/幅度副词（i2v 规则强校验）；**有 dialogue 的镜头 motion 必须含明显的说话/口型动作（speaks naturally / talking while doing X），并且 subject 必须保持说话人开口；无台词则 speaking 留空**；相邻镜 shot_size 必须不同档；主角锚定逐字一致（第一镜的 subject 后续镜逐字复制）；相邻镜间 cause/effect 必须可拍（动作接力/视线/光线）；**相邻镜头 camera 必须换机位（dolly/truck/crane/pedestal 交替使用，禁连镜重复同一机位）——仅在末镜品牌落版（scene 含 logo/纯色背景）允许 static 收尾**；**narration 逐字复制剧本且全片唯一，禁止把同一句旁白重复给两镜**；**全片 subject/scene/motion/narration 禁止出现 XX/占位符/TBD/TODO 等模板残留，品牌名用剧本真实名称**；末镜为品牌落版（静态画面+大字 logo，motion 写 static product shot with soft light drift，dialogue/speaking 留空）；品牌元素≥3镜。

剧本(含台词):
{script}

主角锚定模板(第一镜 subject 用它,后续逐字复制):{actor_anchor}"""


def _project_dir(project_id: str) -> Path:
    d = PROJECTS_DIR / project_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save(project_id: str, name: str, data) -> Path:
    p = _project_dir(project_id) / name
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    # P2：可回溯产物每次落盘都全量快照（内容哈希去重；回滚走 /restore）
    try:
        from shipin_platform.services.artifact_store import (
            snapshot as _art_snapshot)
        _art_snapshot(p.parent, name.rsplit(".", 1)[0], data)
    except Exception:  # noqa: BLE001 - 版本化失败不阻断主流程
        pass
    return p


def _load(project_id: str, name: str):
    p = _project_dir(project_id) / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ---------------------------------------------------------------------------
# 品牌落版确定性通道（2026-09-21 阶梯实测定论）
# ---------------------------------------------------------------------------
# 5 档真实生成实测：agnes-video-2.5-flash 的 on-screen 品牌文字渲染
# 不可依赖（5 档仅 1 档出品牌且伴随主体消失 VLM_BREAK；其余全 brand_seen
# False）。品牌必须走**确定性通道**——名末镜旁白(TTS) + 字幕烧录(SRT)，
# 这两路 100% 可控。但 LLM 分镜常把 brief 里的「XX咖啡/XX品牌」占位符逐字
# 抄进 narration（e2e 实证：S08 narration='XX咖啡'，S09='享受每一刻'——
# 真品牌名从未进入任何文本通道 → 终验 brand_seen=False）。
# _bind_brand 在分镜过审后立即全字段绑实品牌名，并兜底强制落版镜旁白 =
# 品牌名+slogan（模板已有此硬规则，这里做确定性落地）。

_PH_RE = re.compile(
    r"[Xx]{2,6}(?:咖啡|品牌|茶|饮|食品|奶|cafe|brand|coffee)?|xxx|XXXX")

# 「品牌『XX』」在 product_info 里的常见形态：『』「」“”《》成对出现
_BRAND_LITERAL_RE = re.compile(
    r"品牌[\s]*[「『“\"《]([^」』”\"》]{1,16}?)[」』”\"》]")


def _resolve_brand(brief: Optional[dict] = None) -> str:
    """从 brief 解析唯一可信的品牌名（供绑定/入画/终验三处共用）。

    实测教训（2026-09-21）：旧代码在缺 brand_name 时用「中文字符串 2-12
    个」正则抓 product_info 首段，把「精品咖啡，手工烘焙…」开头的品类词
    当品牌名 → 终验 BRAND_MISSING 判定/落版绑定全部对准错误目标。这里改
    为三级精确解析，宁缺毋滥：
      1) 显式 brand_name / product_name；
      2) product_info 里的「品牌『XX』」字面（广告业务问卷的标准形态）；
      3) 都没有 → ""（不猜测，纯信息展示项目无需品牌门）。
    """
    b = brief or {}
    for k in ("brand_name", "product_name"):
        v = str(b.get(k) or "").strip()
        if v:
            return v
    info = str(b.get("product_info") or "")
    m = _BRAND_LITERAL_RE.search(info)
    return m.group(1).strip() if m else ""


def _bind_brand(storyboard: dict, brief: dict) -> dict:
    """把分镜文本里的 XX* 占位符绑为真实品牌名（幂等），并兜底落版镜口播。

    只处理 'XX' 系占位（LLM 抄简占位的常见形态），不误伤正文；无品牌名时
    原样返回。作用于 narration / dialogue / scene / subject 等全部文本位。
    """
    brand = _resolve_brand(brief)
    if not brand:
        return storyboard
    shots = storyboard.get("shots", [])
    if not isinstance(shots, list):
        return storyboard

    def _sub(text) -> str:
        if not isinstance(text, str):
            return text
        return _PH_RE.sub(brand, text)

    for s in shots:
        d = s.get("dialogue")
        if isinstance(d, dict) and isinstance(d.get("text"), str):
            d["text"] = _sub(d["text"])
        for k in ("narration", "scene", "subject", "spatial", "cause",
                  "effect", "motion", "speaking"):
            s[k] = _sub(s.get(k))

    # 落版镜（末镜）旁白兜底：必须含品牌名（模板规则「末镜旁白=品牌名+slogan」）。
    if shots:
        last = shots[-1]
        narr = str(last.get("narration") or "").strip()
        slogan = str(brief.get("slogan") or "").strip()
        last["narration"] = f"{brand}，{slogan or narr}"[:36] if brand not in narr \
            else narr
    return storyboard


# ---------------------------------------------------------------------------
# 意图传导 helper：brief 的字段必须真的「传导」到下游，而不是被散落常量截断
# ---------------------------------------------------------------------------

_STYLE_BY_TONE = {
    "暖": "warm golden palette, cozy intimate lighting",
    "治愈": "soft warm tones, gentle airy atmosphere",
    "高级": "premium elegant aesthetic, refined subtle lighting",
    "简约": "minimalist clean composition, soft daylight",
    "活力": "bright vibrant colors, energetic dynamic lighting",
    "科技": "sleek cool tones, futuristic clean lighting",
    "清新": "fresh natural colors, bright airy look",
    "复古": "vintage film look, warm nostalgic grade",
}

_VERTICAL_PLATFORMS = ("抖音", "快手", "小红书", "视频号", "douyin", "kuaishou",
                       "xiaohongshu", "rednote", "sph", "竖屏")


def _style_anchor(brief: Optional[dict]) -> str:
    """风格锚派生：显式 style_anchor > brief.tone/creative_direction 语调映射 > 兜底。

    之前的管线只认 style_anchor，用户填的 tone/creative_direction 到不了
    生图 prompt——意图在 brief 即丢失。这里把中文基调映射成英文视觉短语，
    保证用户可感知的「语气」贯穿到提示词。
    """
    b = brief or {}
    explicit = str(b.get("style_anchor") or "").strip()
    if explicit:
        return explicit
    raw = f"{b.get('tone') or ''} {b.get('creative_direction') or ''}"
    for kw, en in _STYLE_BY_TONE.items():
        if kw in raw:
            return f"{en}, cinematic, soft natural light"
    return "cinematic, soft natural light"


def _canvas_for_brief(brief: Optional[dict]) -> tuple[int, int, str]:
    """target_platform → (图像宽, 高, kenburns 输出尺寸)。

    竖屏平台（抖音/快手/小红书/视频号）出 9:16 素材；其余出 16:9。
    这是竖版出片的唯一决策点——首帧图、自末帧、kenburns 落版卡全部
    从这里取画幅，杜绝「brief 说要竖屏、产出却是 16:9 横剪」。
    """
    plat = str((brief or {}).get("target_platform") or "").lower()
    if any(k in plat for k in _VERTICAL_PLATFORMS):
        return 720, 1280, "720x1280"
    return 1280, 720, "1280x704"


def _canvas_size_for(manifest: dict, brief: Optional[dict]) -> str:
    """kenburns 输出尺寸跟随**实际在链素材**的画幅,而非只读 brief。

    stitch 的 xfade 链要求全部入镜尺寸一致。变体复用基准素材池时,媒体
    文件仍是基准当时生成的画幅(如 1280x704),而 brief 可能是竖屏平台
    或新改的画幅参数——此时卡片若按 brief 出 9:16,与池里 16:9 主材
    拼接必然尺寸不匹配报错。所以优先 ffprobe 探测链上镜头生成的 clip,
    其次 master(变体共享 dataRoot 时会引用基准 master),都没有才回退
    brief 画幅决策(全新竖屏项目在 generate 阶段已把 clip 建成 720x1280)。
    """
    shots = (manifest or {}).get("shots") or {}
    for mrec in shots.values():
        cp = mrec.get("clip")
        if cp and Path(cp).is_file():
            size = _ffprobe_size(cp)
            if size:
                return f"{size[0]}x{size[1]}"
    for mrec in shots.values():
        mp = mrec.get("master")
        if mp and Path(mp).is_file():
            size = _ffprobe_size(mp)
            if size:
                return f"{size[0]}x{size[1]}"
    return _canvas_for_brief(brief)[2]


def _ffprobe_size(path: str) -> Optional[tuple[int, int]]:
    """ffprobe 视频/图片宽高；异常/文件缺失返回 None。"""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True, shell=False)
    try:
        s = json.loads(r.stdout)["streams"][0]
        return int(s["width"]), int(s["height"])
    except Exception:  # noqa: BLE001 - 探测失败按未知尺寸处理
        return None


def _normalize_canvas(src: Path, dst: Path, w: int, h: int) -> bool:
    """把任意画幅素材统一到目标画幅（比例不符时中心裁剪+黑边）。

    stitch 的 xfade 链要求全部入镜尺寸一致；AGNES 固定输出 720p 横屏，
    9:16 项目必须显式归一化，否则拼接阶段报尺寸不匹配。尺寸已吻合时
    直接返回 False（不转码，零开销）。
    """
    size = _ffprobe_size(src)
    if size == (w, h):
        return False
    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-vf", vf,
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", str(dst)],
        capture_output=True, text=True, shell=False)
    return r.returncode == 0 and dst.exists()


def _fit_duration_to_target(data: dict, target: float) -> tuple[dict, bool, dict]:
    """将剧本总时长等比逼近 brief 目标（tol 12%），返回 (data, 可达?, meta)。

    原管线不比对 brief.duration_sec——用户在表单写「30 秒」，剧本却
    播 48 秒，成片时长完全不受意图约束。这里按比例缩放每镜（钳制在
    2.0-8.0s 的可拍区间），缩放后仍够不着的（时长差太多钳完还差）返回
    不可达，由调用方把该 finding 升级为 critical 阻断。

    轮25:meta 外泄 fitted/dev——_iterate 的时长闭环收尾块曾直接引用
    未定义的 fitted_total/dev/llm_meta,剧本审查循环每轮必崩 NameError
    (实跑复现),整条剧本审查链路形同不存在。
    """
    def _fitted_total(d: dict) -> float:
        return sum(float(s.get("duration_sec") or 0)
                   for s in (d.get("shots") or []) if isinstance(s, dict))

    shots = data.get("shots")
    if not isinstance(shots, list) or not shots:
        return data, False, {"target": round(target, 1), "fitted": 0.0,
                             "dev": 1.0, "reachable": False}
    total = sum(float(s.get("duration_sec") or 0) for s in shots
                if isinstance(s, dict))
    if total <= 0:
        return data, False, {"target": round(target, 1), "fitted": 0.0,
                             "dev": 1.0, "reachable": False}
    dev = abs(total - target) / max(target, 0.001)
    if dev <= 0.12:
        return data, True, {"target": round(target, 1),
                            "fitted": round(total, 1),
                            "dev": round(dev, 3), "reachable": True}
    factor = target / total
    new_shots = []
    for s in shots:
        d = float(s.get("duration_sec") or 0) * factor
        new_shots.append({**s, "duration_sec": round(min(max(d, 2.0), 8.0), 1)})
    fitted = sum(s["duration_sec"] for s in new_shots)
    reachable = abs(fitted - target) / max(target, 0.001) <= 0.12
    out = {**data, "shots": new_shots}
    if isinstance(data.get("duration_sec"), (int, float)):
        out["duration_sec"] = round(fitted, 1)
    return out, reachable, {"target": round(target, 1),
                            "fitted": round(fitted, 1),
                            "dev": round(abs(fitted - target)
                                         / max(target, 0.001), 3),
                            "reachable": reachable}


SCRIPT_REPAIR_PROMPT = """你是严格的 TVC 编剧。下面是一份剧本 JSON 和审片意见。逐条修复所有"必改"项(其余内容与字段保持原样不动),输出修复后的完整剧本 JSON(同一 schema,无 markdown、无解释)。

剧本:
{data}

审片意见(逐条修复):
{plan}"""

STORYBOARD_REPAIR_PROMPT = """你是严格的 TVC 分镜师。下面是一份分镜 JSON 和审片意见。逐条修复所有"必改"项(其余内容与字段保持原样不动),输出修复后的完整分镜 JSON(同一 schema,无 markdown、无解释)。

分镜:
{data}

审片意见(逐条修复):
{plan}"""


def _repair(kind: str, data: dict, revision_plan: list[str]) -> Optional[dict]:
    """按审片意见定向修复(不是盲重生--盲重生成会引入新问题)。"""
    tpl = SCRIPT_REPAIR_PROMPT if kind == "script" else STORYBOARD_REPAIR_PROMPT
    prompt = tpl.replace("{data}", json.dumps(data, ensure_ascii=False)
                         ).replace("{plan}", "\n".join(revision_plan[:12]))
    return _llm_json(prompt, max_tokens=5000)


def _llm_json(prompt: str, max_tokens: int = 4000, retries: int = 2) -> Optional[dict]:
    """服务端 LLM 调用 + 严格 JSON 解析(失败收紧重试)。"""
    from shipin_platform.review.llm_review import _ask_llm, _llm_key
    key = _llm_key()
    if not key:
        return None
    raw = None
    for attempt in range(retries + 1):
        try:
            p = prompt if attempt == 0 else prompt + \
                "\n\n再次强调:只输出一个 JSON 对象本身,不要 markdown 代码块与解释。"
            raw = _ask_llm(p, key, max_tokens=max_tokens)
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                return json.loads(m.group(0))
        except Exception:
            continue
    return None


def _iterate(stage: str, data: dict, project_id: str, store,
             use_llm: bool = True, max_rounds: int = 3) -> dict:
    """审核循环(与 /api/review/iterate 同逻辑 + 状态机记录)。"""
    from shipin_platform.contracts import stable_artifact_hash
    engine = ReviewEngine({"max_rounds": {stage: max_rounds}})
    data = json.loads(json.dumps(data, ensure_ascii=False))
    rounds = []
    brief_ctx = None
    if stage == "script":
        b = _load(project_id, "brief.json")
        brief_ctx = b
    # 意图闭环:剧本总时长必须贴合 brief.duration_sec(±12%)。
    # 可达性在收敛后复查,钳制到极限仍不达标 → 升级 critical 阻断。
    dur_target = 0.0
    dur_reachable = True
    dur_meta: dict = {}
    if stage == "script" and brief_ctx:
        try:
            dur_target = float(brief_ctx.get("duration_sec") or 0)
        except (TypeError, ValueError):
            dur_target = 0.0
    llm_info = {}
    for round_num in range(1, max_rounds + 1):
        if dur_target > 0:
            data, dur_reachable, dur_meta = _fit_duration_to_target(
                data, dur_target)
        report = engine.run_review(stage, data, round_num=round_num)
        rounds.append(report.to_dict())
        if report.decision.value in ("pass", "pass_with_warnings", "stall", "stop"):
            break
        fix = engine.revision.fix(stage, data, report)
        data = fix["data"]
    final = rounds[-1]
    if use_llm and stage in ("script", "storyboard"):
        llm = llm_stage_review(stage, data, brief=brief_ctx)
        if llm["available"]:
            llm_info = {"scores": llm.get("scores", {}), "n": len(llm["findings"])}
            for f in llm["findings"]:
                cls = engine.classifier.classify(stage, f["issue"], f["evidence"])
                final["findings"].append({
                    "dimension": f"llm_{f['dimension']}", "severity": f["severity"],
                    "issue": f["issue"], "evidence": f["evidence"],
                    "failure_mode": cls["mode"], "revision_strategy": cls["strategy"],
                    "proposed_fix": f["fix"], "status": "pending"})
            final["stats"]["critical"] = sum(
                1 for f in final["findings"] if f["severity"] == "critical")
            if final["stats"]["critical"] > 0 and final["decision"] in ("pass", "pass_with_warnings"):
                final["decision"] = "revise"
        else:
            # 轮28:语义审片不可用(key 中途失效/端点 5xx/输出两次不可解析)
            # 时,旧代码静默降级——纯规则 pass 就进花钱的生成阶段,故事四拍
            # 结构/单动作可拍性/镜间连续性/主角一致性/品牌贯穿整轮无人审。
            # 「审不了」≠「审过了」:记 warning finding + 决策不得 pass
            # (revise 重试;max_rounds 后 stop 人工介入),reason 落 llm_info。
            llm_info = {"available": False,
                        "reason": str(llm.get("reason") or "")[:200]}
            final["findings"].append({
                "dimension": "llm_review", "severity": "warning",
                "issue": (f"语义审片不可用({llm_info['reason']})——本阶段只过了"
                          f"规则引擎,故事结构/可拍性/连续性/主角一致性未经语义审查"),
                "evidence": "llm_stage_review available=False",
                "failure_mode": "external", "revision_strategy": "retry",
                "proposed_fix": "检查 AGNES_KEY 与端点可用性后重跑本阶段审查",
                "status": "pending"})
            if final["decision"] in ("pass", "pass_with_warnings"):
                final["decision"] = "revise"

        # LLM critical 的 fix 同步进 revision_plan（否则 _repair 只修规则项，
        # 语义项原样保留 → 纯 LLM 循环不收敛，real-guard-8 实证）
        if use_llm:
            for f in final["findings"]:
                if f.get("severity") == "critical" and f.get("proposed_fix"):
                    final["revision_plan"].insert(
                        0, f"[LLM审片] {f['dimension']}: {f['issue'][:80]} "
                           f"→ 修复: {f['proposed_fix'][:150]}")

    # 时长意图闭环收尾：钳制后仍凑不拢 brief 目标（如 10s 目标被钳到
    # 8s×3 镜=24s）→ 升级 critical 阻断，绝不静默放行与用户时长意图
    # 偏差 >12% 的剧本。
    if dur_target > 0:
        _fitted_total = float(dur_meta.get("fitted") or 0)
        _dev = float(dur_meta.get("dev") or 0)
        if not dur_reachable:
            final["findings"].append({
                "dimension": "duration", "severity": "critical",
                "issue": f"总时长拟合后仍为{_fitted_total:.1f}s,目标{dur_target:.1f}s(偏差{_dev:.0%}),单镜 2-8s 限度内无法收敛",
                "evidence": f"target={dur_target}, fitted={_fitted_total}",
                "failure_mode": "manual", "revision_strategy": "restructure",
                "proposed_fix": "缩短台词/拆分镜头或调整 brief duration_sec 后再提交",
                "status": "pending"})
            final["stats"]["critical"] = (
                final["stats"].get("critical", 0) + 1)
            if final["decision"] in ("pass", "pass_with_warnings"):
                final["decision"] = "revise"
        final["metadata"] = {**(final.get("metadata") or {}),
                             "duration": dict(dur_meta, reachable=dur_reachable)}

    decision = final["decision"]
    h = stable_artifact_hash(data)
    if decision in ("pass", "pass_with_warnings"):
        store.record_artifact(project_id, stage, h)
    else:
        store.record_artifact(project_id, stage, h, status="BLOCKED")
    _save(project_id, f"{stage}.json", data)
    _save(project_id, f"{stage}_review.json",
          {"decision": decision, "rounds": rounds, "llm": llm_info})
    return {"stage": stage, "decision": decision, "data": data,
            "stats": final["stats"], "revision_plan": final.get("revision_plan", []),
            "llm": llm_info}


# ── Phase 1: TEXT ─────────────────────────────────────────────────────


def _script_prompt(brief_data: dict, category: Optional[str] = None) -> str:
    """按品类组装剧本 prompt。

    缺省 category / tvc / 未注册品类 → 返回官方 SCRIPT_PROMPT(逐字节
    不变,保证既有管线行为零偏差);显式品类用模板硬规则拼装。
    模板注册表异常只回退默认,绝不阻塞生成链路。
    """
    brief_json = json.dumps(brief_data, ensure_ascii=False)
    if not category:
        return SCRIPT_PROMPT.replace("{brief}", brief_json)
    try:
        tpl = get_template_registry().get(category)
    except Exception:
        return SCRIPT_PROMPT.replace("{brief}", brief_json)
    if tpl.category == "tvc" or not tpl.rules:
        return SCRIPT_PROMPT.replace("{brief}", brief_json)
    schema = ('{"duration_sec": <int>, "shots": [{"shot_id": "S01", '
              '"duration_sec": <2-5>, "narration": "<一句≤14字的画外旁白,可空串>", '
              '"dialogue": {"role_code": "hero_male|colleague_male|assistant_female", '
              '"text": "<该角色在镜头内说出口的台词,≤20字>"} 或省略, '
              '"scene": "<单一地点单一事件,≤30字>"}]}')
    return (
        f"你是严格的{tpl.name}编剧。依据 brief 创作剧本,输出严格 JSON"
        f"(无 markdown、无解释):\n{schema}\n{tpl.rules}\n"
        f"落版镜专项:{tpl.landing_note}\n\nbrief:\n{brief_json}")


def run_text_phase(project_id: str, brief: dict, store,
                   category: Optional[str] = None) -> dict:
    """brief→剧本→分镜 全部服务端生成+审核。LLM 生成失败或审核不收敛时
    返回 blocked 与原因(agent 只转述,不自行创作)。"""
    steps = []
    # 前置检查:LLM key 未配置 → 明确报「未配置」而非 6 次生成失败后
    # 报「审核未通过」——后者浪费时间且误导（真实原因是配置缺位）。
    from shipin_platform.review.llm_review import _llm_key as _runner_llm_key
    if not _runner_llm_key():
        return {"ok": False, "phase": "text", "steps": steps,
                "reason": "服务端未配置 LLM API Key(AGENTS_KEY 或 OPENAI_API_KEY)"
                          "——生成的文案/分镜需要服务端大模型,配置后重试 /api/pipeline/text"}
    # brief 审核(无 LLM)
    b = _iterate("brief", brief, project_id, store, use_llm=False)
    steps.append({"stage": "brief", "decision": b["decision"]})
    if b["decision"] not in ("pass", "pass_with_warnings"):
        return {"ok": False, "phase": "text", "steps": steps,
                "reason": f"brief 审核不通过: {b['stats']}"}
    _save(project_id, "brief.json", b["data"])

    # 剧本:LLM 生成 → 审核循环(revise 时按 revision_plan 定向修复,非盲重生)
    script = None
    draft = None
    for attempt in range(6):
        if draft is None:
            draft = _llm_json(_script_prompt(b["data"], category))
        else:
            draft = _repair("script", draft, last_plan) or draft
        if not draft or "shots" not in draft:
            draft = None
            continue
        for i, s in enumerate(draft.get("shots", [])):
            s.setdefault("shot_id", f"S{i+1:02d}")
        r = _iterate("script", draft, project_id, store, use_llm=True)
        steps.append({"stage": "script", "attempt": attempt + 1,
                      "decision": r["decision"], "criticals": r["stats"]["critical"]})
        last_plan = r["revision_plan"]
        if r["decision"] in ("pass", "pass_with_warnings"):
            script = r["data"]
            break
    if script is None:
        return {"ok": False, "phase": "text", "steps": steps,
                "reason": "剧本 6 次生成/修复未通过审核(LLM+规则),需要人工介入"}

    # 分镜:同样定向修复。主角锚定优先取 brief.actor_anchor/hero_anchor/
    # character(用户在产品里写了「男主角45岁」「穿蓝西装」就要用上),
    # 无显式锚定才回落默认人设——不再把「25岁女主演」写死进每部片。
    actor_anchor = (str(brief.get("actor_anchor")
                        or brief.get("hero_anchor")
                        or brief.get("character") or "").strip()
                    or "主角为 25 岁左右年轻女性,黑色长直发披肩、米白色针织开衫、深灰色围巾")
    storyboard = None
    draft = None
    last_plan = []
    for attempt in range(6):
        if draft is None:
            draft = _llm_json(STORYBOARD_PROMPT.replace("{script}",
                              json.dumps(script, ensure_ascii=False))
                              .replace("{actor_anchor}", actor_anchor),
                              max_tokens=5000)
        else:
            draft = _repair("storyboard", draft, last_plan) or draft
        if not draft or "shots" not in draft:
            draft = None
            continue
        for s in draft.get("shots", []):
            _src = next((x for x in script["shots"]
                         if x["shot_id"] == s["shot_id"]), {})
            s.setdefault("narration", _src.get("narration", ""))
            dlg = _src.get("dialogue")
            s.setdefault("dialogue", dlg if isinstance(dlg, dict) else "")
        r = _iterate("storyboard", draft, project_id, store, use_llm=True)
        steps.append({"stage": "storyboard", "attempt": attempt + 1,
                      "decision": r["decision"], "criticals": r["stats"]["critical"]})
        last_plan = r["revision_plan"]
        if r["decision"] in ("pass", "pass_with_warnings"):
            storyboard = r["data"]
            # 占位符规避：LLM 会把 brief 里的「XX咖啡」抄进分镜(2026-09-21
            # e2e 实证)，这里绑为真实品牌名再落盘；无品牌名时幂等不变。
            storyboard = _bind_brand(storyboard, b["data"])
            break
    if storyboard is None:
        return {"ok": False, "phase": "text", "steps": steps,
                "reason": "分镜 6 次生成/修复未通过审核,需要人工介入"}

    return {"ok": True, "phase": "text", "steps": steps,
            "script": script, "storyboard": storyboard,
            "confirm_required": ["script", "storyboard"]}


# ── Phase 2: GENERATE ─────────────────────────────────────────────────


def _shot_size(s: dict) -> str:
    v = str(s.get("shot_size") or "ms").strip().lower()
    for k in SIZE_ORDER:
        if k in v:
            return k
    return "ms"


def _scene_token(s: dict) -> str:
    scene = str(s.get("scene") or "")
    for tok in LOCATION_TOKENS:
        if tok in scene:
            return tok
    return scene[:6]


def plan_keyframes(storyboard: dict) -> list[dict]:
    """首尾帧链式策略(coffee-v6 实证校准):

    - 相邻两镜**同一位置**(场景词相同;「同一X」回指继承上一镜位置)→
      链式:last_frame = 下一镜首帧,边界硬切(动作无缝接力)。v6 实证
      景别跨档(cu→ms 等)链式照样成功,位置跳变才是真断点;
    - 换位置 / 落版 → 自末帧(边界 dissolve);末镜不参与出边界。
    返回 [{shot_id, mode: chain|own_end|outro_card, boundary}]。
    """
    shots = storyboard["shots"]
    plan = []
    prev_loc = None
    for i, s in enumerate(shots):
        scene = str(s.get("scene") or "")
        if "同一" in scene and prev_loc:
            loc = prev_loc
        else:
            loc = _scene_token(s)
            prev_loc = loc
        is_outro = bool(re.search(r"落|out|版|束", str(s.get("beat") or ""), re.I)) \
            and i == len(shots) - 1
        if i == len(shots) - 1:
            plan.append({"shot_id": s["shot_id"],
                         "mode": "outro_card" if is_outro else "own_end",
                         "boundary": "dissolve", "loc": loc})
            continue
        nxt = shots[i + 1]
        nxt_scene = str(nxt.get("scene") or "")
        nxt_loc = loc if "同一" in nxt_scene else _scene_token(nxt)
        if nxt_loc == loc:
            plan.append({"shot_id": s["shot_id"], "mode": "chain",
                         "boundary": "cut", "loc": loc})
        else:
            plan.append({"shot_id": s["shot_id"], "mode": "own_end",
                         "boundary": "dissolve", "loc": loc})
    return plan


def run_generate_phase(project_id: str, store, workdir: Optional[str] = None) -> dict:
    """首帧图 → 首尾帧链 → 锚定视频 → QC(重试≤2)→ TTS → 对齐。
    全程确定性;LLM 不参与。返回逐镜报告。"""
    gates = []
    for gate in ("script", "storyboard"):
        try:
            store.assert_confirmed(project_id, gate)
        except Exception as e:
            return {"ok": False, "phase": "generate", "reason": str(e)}
        try:
            store.assert_stage_pass(project_id, gate)
        except Exception as e:
            return {"ok": False, "phase": "generate", "reason": str(e)}
    storyboard = _load(project_id, "storyboard.json")
    brief0 = _load(project_id, "brief.json") or {}
    storyboard = _bind_brand(storyboard, brief0)
    # 绑后落盘：assemble/UI 读盘即得规范版，不依赖再跑 generate 时重绑
    _save(project_id, "storyboard.json", storyboard)

    # 提示词:从分镜确定性派生(无 LLM,无漂移空间)。放 generate 阶段:
    # BLOCKED 时本阶段会自动重派生并重审(动词表扩容后自愈)。
    style = _style_anchor(_load(project_id, "brief.json"))
    canvas_w, canvas_h, kb_size = _canvas_for_brief(
        _load(project_id, "brief.json"))
    brief0 = _load(project_id, "brief.json")
    brand_name0 = _resolve_brand(brief0)
    # C 变体实测(2026-09-21):提示词里给出品牌名+落点(杯身/物件)
    # 即可驱动品牌入画(brand_seen False→True)。无品牌名不注入。
    brand_shot = (f" The brand name {brand_name0!r} printed on a small "
                  f"product label or cup, readable, softly lit." if brand_name0 else "")
    img_prompts, vid_prompts = [], []
    _n_shots = len(storyboard["shots"])
    for _i, s in enumerate(storyboard["shots"]):
        # 品牌入画只放首/尾镜(广告惯例:开场亮牌、结尾收牌);中段镜头
        # 不加,否则品牌文字在每镜都出现显假。
        _brand = brand_shot if (_i == 0 or _i == _n_shots - 1) else ""
        dlg = s.get("dialogue")
        spk = (str(s.get("speaking") or "").strip()
               if isinstance(dlg, dict) and dlg.get("text") else "")
        speaking_en = f" {spk}." if spk else ""
        img_prompts.append({"shot_id": s["shot_id"], "prompt_en":
                            f"{s['subject']}. {s['motion']}{speaking_en} "
                            f"Scene: {s['scene']}. "
                            f"{s['spatial']}. {s['camera']}. {style}, no text"})
        vid_prompts.append({"shot_id": s["shot_id"],
                            # 模板升级(2026-09-21 A/B 实测):A(中文短模板)在 2.5s
                            # 出现主体变形;英文长模板(scene+固定机位+景深+主体居中
                            # +禁止形变)verdict=pass 且运动能量 ×2。赢点固化在这里:
                            "prompt_text": (f"{s['motion']}{speaking_en} "
                                            f"Scene: {s['scene']}. "
                                            f"Camera: {s['camera']}, fixed at "
                                            f"medium close-up, shallow depth of "
                                            f"field, subject stays centered. "
                                            f"One single continuous take — no "
                                            f"camera cut, no scene change, no "
                                            f"morphing or shape change of the "
                                            f"subject, natural stable motion, "
                                            f"smooth ending."
                                            f"{_brand}")})
    for stage, data in (("image_prompt", {"style_anchor": style, "shot_prompts": img_prompts}),
                        ("video_prompt", {"shot_prompts": vid_prompts})):
        row = store.get_stage(project_id, stage)
        # 轮29:PASS 缓存必须过内容哈希——旧逻辑只看 status==PASS 就整段
        # 跳过,分镜文本变了(shot_id 集合不变,如 _bind_brand 绑实品牌/
        # 用户 rewrite)时新派生 prompt 被丢弃、生成用旧 prompt,品牌注入
        # 丢失要等终审 BRAND_MISSING 才炸(钱已花完),且 prompt 阶段对
        # 新输入再无审查。派生数据与已审哈希一致=真缓存命中才跳过。
        if _prompt_stage_stale(row, data):
            pass  # 走下面重审
        elif row is not None and row["status"] == "PASS":
            continue
        r = _iterate(stage, data, project_id, store, use_llm=False)
        if r["decision"] not in ("pass", "pass_with_warnings"):
            return {"ok": False, "phase": "generate",
                    "reason": f"{stage} 审核不通过: {r['stats']}"}

    ip = _load(project_id, "image_prompt.json")
    shots = storyboard["shots"]
    img_map = {p["shot_id"]: p["prompt_en"] for p in ip["shot_prompts"]}
    vp = _load(project_id, "video_prompt.json")
    vid_map = {p["shot_id"]: p.get("prompt_text") or p.get("prompt_en", "")
               for p in vp["shot_prompts"]}
    work = _project_dir(project_id)
    manifest = _load(project_id, "manifest.json") or {"shots": {}}
    report = []
    NEG = ("cuts, jump cuts, scene transition, multiple shots, montage, "
           "split screen, zoom change, text overlay")

    # 1) 首帧图(缺则生成;变体继承的 manifest 已引用现成素材则直接复用)
    for s in shots:
        sid = s["shot_id"]
        mrec = manifest["shots"].setdefault(sid, {})
        ref = mrec.get("first_frame")
        if ref and Path(ref).is_file():
            continue  # 素材池命中(base dataRoot 共享),无需重生成
        fp = work / f"{sid}.jpg"
        if not fp.exists():
            r = generate_image_agnes(img_map[sid], canvas_w, canvas_h, str(fp))
            if not r.get("ok"):
                return {"ok": False, "phase": "generate",
                        "reason": f"{sid} 首帧图生成失败", "report": report}
            record_cost(project_id, "image", model="agnes-image",
                        units=1.0, note=f"{sid} 首帧")
        manifest["shots"][sid]["first_frame"] = str(fp)

    # 2) 首尾帧策略 + 自末帧图
    plan = plan_keyframes(storyboard)
    manifest["keyframe_plan"] = plan
    by_id = {p["shot_id"]: p for p in plan}
    for i, s in enumerate(shots):
        sid = s["shot_id"]
        mode = by_id[sid]["mode"]
        if mode == "chain":
            nxt = shots[i + 1]["shot_id"]
            manifest["shots"][sid]["last_frame"] = str(work / f"{nxt}.jpg")
        elif mode in ("own_end", "outro_card"):
            ref_lp = manifest["shots"][sid].get("last_frame")
            lp = work / f"{sid}_last.jpg"
            if ref_lp and Path(ref_lp).is_file():
                pass  # 素材池命中,沿用基准末帧
            elif not lp.exists() and mode == "own_end":
                base = img_map[sid]
                r = generate_image_agnes(base + " , the action completed, end state of this exact shot, same framing and lighting", canvas_w, canvas_h, str(lp))
                if not r.get("ok"):
                    return {"ok": False, "phase": "generate",
                            "reason": f"{sid} 末帧图生成失败", "report": report}
                record_cost(project_id, "image", model="agnes-image",
                            units=1.0, note=f"{sid} 自末帧")
            manifest["shots"][sid]["last_frame"] = str(ref_lp) if (ref_lp and Path(ref_lp).is_file()) else str(lp)
        manifest["shots"][sid]["boundary"] = by_id[sid]["boundary"]

    # 2.5) image_gen 落账:哈希 = 首/末帧文件路径+字节数(内容重生成则失效)。
    # 之前 image_gen 阶段从不 record_artifact → finalize 的 required=…video_gen
    # 永远 NOT_STARTED,发布硬闸形同虚设。这里把每镜关键帧的实体指纹打进
    # 状态机,重跑/替换素材都会让 hash 变化。
    from shipin_platform.contracts import stable_artifact_hash as _sah
    img_fp = {}
    for sid, mrec in manifest["shots"].items():
        for k in ("first_frame", "last_frame"):
            p = mrec.get(k)
            fp = Path(p) if p else None
            img_fp[f"{sid}:{k}"] = (
                str(fp) if fp and fp.is_file() else "",
                fp.stat().st_size if fp and fp.is_file() else -1)
    store.record_artifact(project_id, "image_gen", _sah(img_fp))

    # 3) 视频 + QC + 重试(落版卡留给 assemble 的 kenburns,不跑 agnes)
    # 轮12:单镜 VLM 符合度诊断结果按镜累积(缓存镜沿用旧条目)
    shots_review = _load(project_id, "shots_review.json") or {}
    for i, s in enumerate(shots):
        sid = s["shot_id"]
        mrec = manifest["shots"][sid]
        if by_id[sid]["mode"] == "outro_card":
            report.append({"shot_id": sid, "note": "kenburns 落版卡,assemble 阶段生成"})
            continue
        if mrec.get("qc") == "ok":
            report.append({"shot_id": sid, "qc": "ok", "cached": True})
            continue
        dur = float(s.get("duration_sec") or 3)
        clip = work / f"{sid}_clip.mp4"
        # 轮21:关键帧 vs 分镜文本门——视频模型以 first_frame 为条件
        # 生成,关键帧跑偏整镜必歪,而 qc_clip 的 dHash 只比「clip 首帧
        # vs 参考图」(同源几乎必然一致),没人审过参考图本身。视频生成
        # 前先审图:每镜审一次(关键帧路径变化即重审),critical 并入
        # shots_review 既有合并通道由终审统一阻断。
        _kf_stamp = str(mrec.get("first_frame") or "")
        if mrec.get("keyframe_review_of") != _kf_stamp:
            try:
                from shipin_platform.review.hard_gates import check_keyframes
                _kf = check_keyframes([{
                    "shot_id": sid,
                    "first_frame": mrec.get("first_frame"),
                    "subject": s.get("subject"),
                    "scene": s.get("scene")}])
                mrec["keyframe_review_of"] = _kf_stamp
                mrec["keyframe_review"] = _kf.get("verdict")
                if _kf.get("findings"):
                    _sr = _load(project_id, "shots_review.json") or {}
                    _sr[f"__keyframe_{sid}__"] = {
                        "verdict": _kf.get("verdict"),
                        "findings": _kf.get("findings") or []}
                    _save(project_id, "shots_review.json", _sr)
            except Exception as e:
                mrec["keyframe_review"] = f"error: {str(e)[:120]}"
        attempts = []
        for attempt in range(3):
            # 重试阶梯(v6 实证):1) 原提示词 2) 固定机位+原运动
            # 3) 固定机位+主体极简运动。机位运动+主体运动叠加是切镜主诱因。
            if attempt == 0:
                prompt = vid_map.get(sid, s["motion"])
            elif attempt == 1:
                prompt = (f"ONE single uninterrupted take, FIXED camera, no camera "
                          f"movement at all. {s['motion']}")
            else:
                prompt = (f"ONE single uninterrupted take, FIXED camera, no camera "
                          f"movement. The subject moves minimally: {s['motion']}")
            net_retries = 0
            r = None
            while True:
                try:
                    r = generate_video_agnes(
                        prompt=prompt + " Single continuous take, no cuts.",
                        duration=int(max(dur, 2)), resolution="720p",
                        first_frame=mrec["first_frame"], last_frame=mrec["last_frame"],
                        output_path=str(clip),
                        negative_prompt=NEG)
                    break
                except Exception as e:
                    net_retries += 1
                    attempts.append({"attempt": attempt + 1, "net_retry": net_retries,
                                     "error": str(e)[:120]})
                    if net_retries >= 2:
                        break
            if r is None:
                continue
            mrec["master"] = r.get("master_path")
            record_cost(project_id, "video", model="agnes-video",
                        units=round(max(dur, 2), 1), note=sid)
            qc = qc_clip(str(clip), shot_id=sid,
                         expected_duration_sec=dur,
                         reference_image=mrec["first_frame"],
                         use_vlm=True)  # M5(2026-09-21 审计):dHash 盲区用 VLM 补
            attempts.append({"attempt": attempt + 1, "qc": qc["verdict"],
                             "cuts": qc["checks"]["internal_cuts"]["value"]})
            if qc["verdict"] == "ok":
                mrec["qc"] = "ok"
                # 画布归一化:AGNES 固定出 720p 横屏,9:16 项目必须统一到目标
                # 画幅(中心裁剪+黑边),否则 stitch 的 xfade 链因尺寸不一致失败;
                # master 也要同步归一化(转场会从 master 借帧补时长)。
                cvp = work / f"{sid}_canvas.mp4"
                if _normalize_canvas(str(clip), cvp, canvas_w, canvas_h):
                    clip = str(cvp)
                if (mrec.get("master") and Path(mrec["master"]).is_file()
                        and _normalize_canvas(Path(mrec["master"]),
                                              work / f"{sid}_canvas_master.mp4",
                                              canvas_w, canvas_h)):
                    mrec["master"] = str(work / f"{sid}_canvas_master.mp4")
                mrec["clip"] = str(clip)
                # 审计 G5:clip 内容哈希落账——assemble 时代验同一文件名是否
                # 被换过内容(拼接阶段会按此清单逐片核对,防"审A拼B")
                import hashlib as _hl
                try:
                    mrec["clip_sha256"] = _hl.sha256(Path(clip).read_bytes()).hexdigest()
                except OSError:
                    mrec.pop("clip_sha256", None)
                store.record_clip_qc(project_id, sid, str(clip), "ok", {"attempts": attempts})
                # 轮12:单镜 VLM 符合度诊断——每镜独立 ctx 逐帧对照分镜
                # 文本预期(终审是一个 prompt 扛全部分镜,镜头一多预期被稀释,
                # coffee-v7 实测 S05/S08 判定漂移)。只记录不拦生成:clip 过审
                # ≠进终片的内容(assemble 窗口不足时从 master 补帧),critical
                # 在 assemble 并入终审统一阻断(与 timeline 门同一范式)。
                try:
                    from shipin_platform.review.hard_gates import vlm_review_shot
                    _sr = vlm_review_shot(str(clip), s, frames_count=4)
                    shots_review[sid] = {"verdict": _sr.get("verdict"),
                                         "findings": _sr.get("findings") or []}
                except Exception as e:
                    shots_review[sid] = {"verdict": "error", "findings": [],
                                         "error": str(e)[:160]}
                _save(project_id, "shots_review.json", shots_review)
                _save(project_id, "manifest.json", manifest)
                break
            mrec["qc"] = "fix"
        store.record_clip_qc(project_id, sid, str(clip),
                             "ok" if mrec.get("qc") == "ok" else "fix", {"attempts": attempts})
        report.append({"shot_id": sid, "qc": mrec.get("qc"), "attempts": attempts})
        if mrec.get("qc") != "ok":
            _save(project_id, "manifest.json", manifest)
            return {"ok": False, "phase": "generate", "reason": f"{sid} 3 次生成未通过 QC",
                    "report": report}

    # 4) TTS(缺则生成;变体沿用的素材引用直接复用)+ 对齐
    from shipin_platform.services.tts_service import create_tts_service
    tts = create_tts_service(work, ROOT / "data" / "voice_cast.db")
    def _tts_of(sid: str) -> Optional[str]:
        local = glob_tts(work, sid)
        if local:
            return local
        ref = (manifest["shots"].get(sid) or {}).get("tts")
        return ref if (ref and Path(ref).is_file()) else None

    def _tts_fresh(sid: str, s: dict) -> Optional[str]:
        """轮26:复用必须过文本指纹——台词变了旧音频作废,不能旧词配
        新字幕(声画不一致违反剧本,且无门能发现)。"""
        p = _tts_of(sid)
        if not p:
            return None
        rec = manifest["shots"].get(sid) or {}
        if str(rec.get("tts_text_sha") or "") != _tts_text_sha(s):
            return None
        return p

    need = [s["shot_id"] for s in shots if not _tts_fresh(s["shot_id"], s)]
    if need:
        segs = []
        # 台词镜:角色先开口(role_code→音色),旁白随后跟;无台词镜只有旁白
        for s in shots:
            if s["shot_id"] not in need:
                continue
            dlg = s.get("dialogue")
            if isinstance(dlg, dict) and dlg.get("text"):
                role = str(dlg.get("role_code") or "biz_female")
                segs.append(tts.build_segment(f"{s['shot_id']}_dlg", dlg["text"],
                                              role_code=role))
                if s.get("narration"):
                    segs.append(tts.build_segment(s["shot_id"], s["narration"],
                                                  role_code="biz_female"))
            else:
                segs.append(tts.build_segment(s["shot_id"], s["narration"],
                                              role_code="biz_female"))
        _synth = tts.synthesize_segments_sync(segs)
        record_cost(project_id, "tts", model="tts-v1",
                    units=float(len(segs)), note="旁白+台词")
        # 轮31:合成失败必须显式失败——synthesize 的失败是静默的(段上挂
        # error/output_path 空),旧代码丢弃返回值:_tts_of 按 mtime 取到
        # 上一轮的旧音频,随后新文本指纹盖上去 → 该镜从此对指纹永远"新鲜",
        # 无限复用旧口播(旧词配新字幕,正是轮26 要消灭的声画不一致),
        # 且 align 按错音频算窗口、全链路无门能发现。
        _failed = _tts_failures(_synth)
        if _failed:
            return {"ok": False, "phase": "generate",
                    "reason": ("TTS 合成失败(旧音频已作废,拒绝盖新指纹): "
                               + "; ".join(f"{s.shot_id}:{str(s.error)[:60]}"
                                           for s in _failed[:3]))}
    for s in shots:
        p = _tts_of(s["shot_id"])
        if not p:
            return {"ok": False, "phase": "generate", "reason": f"{s['shot_id']} TTS 缺失"}
        manifest["shots"][s["shot_id"]]["tts"] = p
        # 轮26:记下口播内容指纹——下次重跑凭它判断旧音频是否还有效
        manifest["shots"][s["shot_id"]]["tts_text_sha"] = _tts_text_sha(s)
        dlg = s.get("dialogue")
        if isinstance(dlg, dict) and dlg.get("text"):
            dv = glob_tts(work, f"{s['shot_id']}_dlg")
            if dv:
                manifest["shots"][s["shot_id"]]["dlg"] = dv
    align = align_narration([{"shot_id": s["shot_id"],
                              "duration_sec": float(s.get("duration_sec") or 3),
                              "narration_path": manifest["shots"][s["shot_id"]]["tts"],
                              "dialogue_path": manifest["shots"][s["shot_id"]].get("dlg")}
                             for s in shots])
    if align["verdict"] != "ok":
        # 对齐失败必须阻断——原代码把 fix verdict 静默写进 manifest 继续
        # assemble,旁白超窗/缺失的片子在拼接阶段才爆或干脆产错位片。
        criticals = [f["message"] for f in align["findings"]
                     if f.get("severity") == "critical"]
        return {"ok": False, "phase": "generate",
                "reason": f"旁白对齐未通过: {'; '.join(criticals)[:240]}",
                "report": report,
                "align": {"verdict": align["verdict"],
                          "findings": align["findings"]}}
    manifest["align"] = align
    _save(project_id, "manifest.json", manifest)
    # video_gen 落账:哈希 = 整份 manifest(含剪辑/QC/TTS/对齐)——assemble 前置
    # 校验会比对同哈希,素材被改动或重新生成后 hash 变化 → 拒绝旧链条混拼。
    store.record_artifact(project_id, "video_gen", _sah(manifest))
    return {"ok": True, "phase": "generate", "report": report,
            "align": {"verdict": align["verdict"], "total_sec": align["total_sec"],
                      "findings": align["findings"]}}


def glob_tts(work: Path, shot_id: str) -> Optional[str]:
    import glob as _g
    fs = sorted(_g.glob(str(work / f"{shot_id}_*.mp3")), key=_mtime)
    # 轮26:台词段输出是 {sid}_dlg_<uuid>.mp3,会被 {sid}_* 通配吞掉——
    # 旁白 glob 必须排除它,否则旁白轨取到台词音频(align 按错音频算
    # 时长、成片声画错配),此类错配无任何门能发现。
    if not shot_id.endswith("_dlg"):
        fs = [f for f in fs
              if not Path(f).name.startswith(f"{shot_id}_dlg")]
    return fs[-1] if fs else None


def _tts_text_sha(s: dict) -> str:
    """轮26:口播内容指纹(旁白+台词文本)。TTS 复用判定从「有没有
    文件」升级为「文本有没有变」——review/iterate 改了 narration 后,
    旧 {sid}_*.mp3 会被直接复用,成片口播是旧词、字幕是新词,
    声画不一致直接违反剧本,且全链路没有门能发现。"""
    import hashlib as _hl3
    dlg = s.get("dialogue")
    dtext = (str(dlg.get("text") or "") if isinstance(dlg, dict)
             else str(dlg or ""))
    payload = f"{str(s.get('narration') or '')}\x00{dtext}"
    return _hl3.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _tts_failures(segments) -> list:
    """轮31:合成失败段——error 非空或 output_path 为空。

    synthesize_segments_sync 的失败是静默的(asyncio.gather 吞异常挂到
    段上),旧调用方丢弃返回值:_tts_of 随后按 mtime 取到上一轮的旧音频,
    新文本指纹盖上去 → 该镜从此对指纹永远「新鲜」,无限复用旧口播
    (旧词配新字幕,正是轮26 要消灭的声画不一致),align 还按错音频算
    窗口,全链路无门能发现。失败段必须让 generate 显式失败。"""
    return [s for s in (segments or [])
            if getattr(s, "error", "") or not getattr(s, "output_path", "")]


def _prompt_stage_stale(row, data: dict) -> bool:
    """轮29:prompt 阶段 PASS 缓存是否已脱钩(输入变了但状态仍 PASS)。

    旧逻辑 `row.status != "PASS"` 才重审——分镜文本变了(shot_id 集合
    不变,如 _bind_brand 绑实品牌名、用户 /rewrite 改主体描述)时,新派生
    prompt 被整个丢弃、生成用盘上的旧 prompt:品牌注入丢失要等终审
    BRAND_MISSING 才炸( generation 钱已花完),且 prompt 阶段对新输入
    再无审查。判据:派生数据哈希 != 已审 artifact_hash → 脱钩,必须重审。
    行缺失/哈希为空(旧数据)视为脱钩(安全方向:重审一遍)。"""
    if row is None:
        return False
    try:
        if str(row["status"]) != "PASS":
            return False
    except (KeyError, IndexError, TypeError):
        return False
    recorded = str((row["artifact_hash"] if "artifact_hash" in row.keys()
                    else "") or "")
    if not recorded:
        return True
    try:
        from shipin_platform.contracts import stable_artifact_hash
        return stable_artifact_hash(data) != recorded
    except Exception:
        return True


def _mtime(p: str) -> float:
    from os.path import getmtime
    return getmtime(p)


def _component_defaults(component_id: str) -> dict:
    """取组件配方默认参数;配方异常回退空(调用方用原常量兜底)。"""
    try:
        return get_component_registry().get(component_id).params()
    except Exception:
        return {}


# ── Phase 3: ASSEMBLE ─────────────────────────────────────────────────


def run_assemble_phase(project_id: str, store) -> dict:
    """对齐→落版卡→逐边界转场拼接→调色→字幕→声音设计→mux→归一化→终验→发布。

    硬闸门:storyboard 必须确认+PASS,且 manifest 与 video_gen 阶段记录的
    artifact_hash 一致——杜绝「改了分镜/换素材不重跑 generate 就拼接」的
    混料出片(v4 病根:assemble 在 storyboard 改写后仍引用旧镜头)。"""
    # ── 闸门:人工确认 → 上游 PASS → manifest 与已验收素材哈希一致 ──
    for gate in ("storyboard",):
        try:
            store.assert_confirmed(project_id, gate)
        except Exception as e:
            return {"ok": False, "phase": "assemble", "reason": str(e)}
        try:
            store.assert_stage_pass(project_id, gate)
        except Exception as e:
            return {"ok": False, "phase": "assemble", "reason": str(e)}
    from shipin_platform.contracts import stable_artifact_hash as _sah
    vg = store.get_stage(project_id, "video_gen")
    if vg is None or vg["status"] != "PASS":
        return {"ok": False, "phase": "assemble",
                "reason": "video_gen 未通过——先跑 generate 阶段生成并验收全部镜头素材"}
    manifest = _load(project_id, "manifest.json")
    storyboard = _load(project_id, "storyboard.json")
    storyboard = _bind_brand(storyboard, _load(project_id, "brief.json") or {})
    if not manifest or not manifest.get("align"):
        return {"ok": False, "phase": "assemble", "reason": "先跑 generate 阶段"}
    if vg["artifact_hash"] != _sah(manifest):
        return {"ok": False, "phase": "assemble",
                "reason": "manifest 内容与 video_gen 验收时不一致(素材/分镜已变更)"
                          "——请重新运行 generate 阶段后再拼接"}
    work = _project_dir(project_id)
    brief = _load(project_id, "brief.json") or {}
    shots = storyboard["shots"]
    tl = manifest["align"]["timeline"]
    sids = [t["shot_id"] for t in tl]
    # 审计 G5:clip 内容哈希一致性——generate 验收过的素材在拼接前必须字节一致,
    # 防「审查通过的是 A 文件,拼接用的是换过的 B 文件」类狸猫换太子
    import hashlib as _hl2
    for _sid in sids:
        _rec = (manifest.get("shots") or {}).get(_sid) or {}
        _exp = _rec.get("clip_sha256")
        if not _exp:
            continue  # 旧项目无哈希记录,不做追溯
        _cp = Path(_clip_src(manifest, _sid, work))
        try:
            if _hl2.sha256(_cp.read_bytes()).hexdigest() != _exp:
                return {"ok": False, "phase": "assemble",
                        "reason": (f"{_sid} 素材内容与 generate 阶段验收时不一致"
                                   f"(clip_sha256 变更)——素材被替换,禁止拼接"),
                        "clip_hash_mismatch": str(_cp)}
        except OSError as _e:
            return {"ok": False, "phase": "assemble",
                    "reason": f"{_sid} 素材读取失败: {_e}"}
    windows = [t["window_sec"] for t in tl]
    # 组件配方默认值(远期3:散落常量升为可覆盖参数;异常时回退原常量)
    comp_outro = _component_defaults("outro_card")
    comp_trans = _component_defaults("transition")
    comp_sub = _component_defaults("subtitle")
    comp_snd = _component_defaults("sound_design")
    out = {}

    # 1) 落版卡:beat 含 落/out 的末镜 → kenburns(窗口+td,供转场借帧)
    last_sid = sids[-1]
    last_shot = next(s for s in shots if s["shot_id"] == last_sid)
    if re.search(r"落|out|版|束", str(last_shot.get("beat") or ""), re.I):
        w9 = windows[-1]
        # 首帧图:先取素材池引用,缺则回退本地约定命名(变体共享 dataRoot 时
        # 落版卡沿用基准首帧,避免为同一画面重新生成)
        kb_src = (manifest["shots"].get(last_sid) or {}).get("first_frame")
        if not kb_src or not Path(kb_src).is_file():
            kb_src = str(work / f"{last_sid}.jpg")
        kb = kenburns(kb_src, round(w9 + DEFAULT_TD, 2),
                      str(work / f"{last_sid}_clip.mp4"),
                      zoom_to=comp_outro.get("zoom_to", 1.18),
                      size=_canvas_size_for(manifest, brief))
        if not kb.get("ok"):
            return {"ok": False, "phase": "assemble", "reason": f"kenburns: {kb.get('error')}"}
        manifest["shots"][last_sid]["clip"] = kb["output"]
        manifest["shots"][last_sid]["master"] = kb["output"]
        out["kenburns"] = kb
        # 轮15:落版镜单镜诊断——outro_card 在 generate 阶段被跳过,kenburns
        # 产物到此才生成,此前从未进入单镜诊断(品牌落版恰是最关键镜头:
        # 终审 BRAND_MISSING 只兜"品牌有没有出现",不兜"落版画面是否
        # 符合分镜")。补一次 focused 审查,critical 走 shots_review 既有
        # 合并通道并入终审。
        try:
            from shipin_platform.review.hard_gates import vlm_review_shot
            _sr = _load(project_id, "shots_review.json") or {}
            _kr = vlm_review_shot(kb["output"],
                                  dict(last_shot,
                                       duration_sec=round(w9 + DEFAULT_TD, 2)),
                                  frames_count=4)
            _sr[last_sid] = {"verdict": _kr.get("verdict"),
                             "findings": _kr.get("findings") or []}
            _save(project_id, "shots_review.json", _sr)
        except Exception as e:
            _sr = _load(project_id, "shots_review.json") or {}
            _sr[last_sid] = {"verdict": "error", "findings": [],
                             "error": str(e)[:160]}
            _save(project_id, "shots_review.json", _sr)

    # 2) 逐边界转场拼接(clip 路径缺失时回退到约定命名)
    clips = [_clip_src(manifest, s, work) for s in sids]
    masters = [manifest["shots"][s].get("master") for s in sids]
    bts = [manifest["shots"][s].get("boundary", "dissolve") for s in sids[1:]]
    st = build_transition_stitch(clips, windows, str(work / "stitched.mp4"),
                                 transition_duration=comp_trans.get(
                                     "transition_duration", DEFAULT_TD),
                                 masters=masters,
                                 boundary_transitions=bts)
    if not st.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"stitch: {st.get('error')}"}
    out["stitch"] = {k: st.get(k) for k in ("duration", "expected_sec",
                                            "boundary_preserved", "warnings")}
    _save(project_id, "stitch_result.json", st)
    # 轮13:part 来源透明化——align 窗口 > clip 时长时 stitch 从 master 裁料
    # 补足,该 part 内容从未过审(clip 审的是另一份),G5 的 clip_sha256 只盯
    # clip 文件管不到。这里对 master 补料的 part 按**实际入拼片段**补单镜
    # 诊断,critical 落 parts_review.json,终审合并时统一阻断。
    _parts = st.get("parts") or []
    _save(project_id, "stitch_parts.json", _parts)
    _parts_review = _load(project_id, "parts_review.json") or {}
    for pm in _parts:
        if pm.get("source") != "master" or not pm.get("part_path"):
            continue
        i = pm.get("idx")
        sid = sids[i] if isinstance(i, int) and i < len(sids) else None
        srow = next((x for x in shots if x["shot_id"] == sid), None)
        if not sid or not srow:
            continue
        try:
            from shipin_platform.review.hard_gates import vlm_review_shot
            pr = vlm_review_shot(pm["part_path"],
                                 dict(srow,
                                      duration_sec=pm.get("want_sec")),
                                 frames_count=4)
            _parts_review[str(i)] = {
                "shot_id": sid, "source": pm.get("source"),
                "part_path": pm.get("part_path"),
                "verdict": pr.get("verdict"),
                "findings": pr.get("findings") or []}
        except Exception as e:
            _parts_review[str(i)] = {"shot_id": sid, "verdict": "error",
                                     "findings": [],
                                     "error": str(e)[:160]}
    if _parts_review:
        _save(project_id, "parts_review.json", _parts_review)

    # 3) 调色
    graded = str(work / "graded.mp4")
    g = color_grade_warm(st["output"], graded)
    if not g.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"color-grade: {g}"}
    out["color_grade"] = {"output": graded}

    # 4) 字幕(服务端按 align 时间轴生成)
    srt = _build_srt(storyboard, tl, manifest)
    srt_path = work / "subs.srt"
    srt_path.write_text(srt, encoding="utf-8")
    burn = burn_srt(str(Path(graded).resolve()), str(srt_path.resolve()),
                    str((work / "subtitled.mp4").resolve()),
                    font_size=int(comp_sub.get("font_size", 46)),
                    margin_v=int(comp_sub.get("margin_v", 96)))
    if not burn.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"burn: {burn.get('error', burn)}"}
    # 轮27:§10.6 字幕验收硬门挂主链路——此前只存在于手工 /burn 端点,
    # assemble 烧完直接放行(violations 恒 None),无墨迹/超宽的不可读
    # 字幕直达终审。found=false 与宽度超红线=不可读缺陷,硬拦;y 带记
    # 警告不拦(验收规则与 margin_v 默认排版的矛盾,见共享函数注释)。
    from shipin_platform.tools.subtitle_renderer import check_subtitle_cues
    _viol = check_subtitle_cues(burn.get("cues") or [],
                                str((work / "subtitled.mp4").resolve()))
    _viol_crit = [v for v in _viol if v.get("severity") == "critical"]
    out["burn"] = {"violations": _viol or None}
    if _viol_crit:
        _save(project_id, "subtitle_check.json",
              {"verdict": "fix", "violations": _viol})
        return {"ok": False, "phase": "assemble",
                "reason": ("字幕验收未过(§10.6): "
                           + "; ".join(f"cue{v['cue']}:{'/'.join(v['issues'])}"
                                       for v in _viol_crit[:3])),
                "burn": out["burn"]}
    _save(project_id, "subtitle_check.json",
          {"verdict": "ok", "violations": _viol})

    # 5) 声音设计：旁白轨 + 台词轨(role 音色) 全部落点,台词在前旁白在后
    events = []
    for s, t_ in zip(sids, tl):
        mrec = manifest["shots"].get(s, {})
        narr_p = mrec.get("tts")
        dlg_p = mrec.get("dlg")
        # 台词先说(镜起点)，旁白随后 (align 已算好 narr_at)
        if dlg_p:
            events.append({"path": dlg_p,
                           "time": t_["dlg_start_sec"] or t_["audio_start_sec"]})
        if narr_p:
            events.append({"path": narr_p, "time": t_["audio_start_sec"]})
    ma = master_audio(None, float(a_total(tl)), str(work / "soundbed.wav"),
                      bgm_path=str(BGM_PATH) if BGM_PATH.exists() else None,
                      bgm_gain_db=float(comp_snd.get("bgm_gain_db", -19.0)),
                      duck=bool(comp_snd.get("duck", True)),
                      narration_events=events,
                      sfx_events=[{"time": t_["audio_start_sec"], "kind": "whoosh"}
                                  for t_ in tl[1:]])
    if not ma.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"master: {ma.get('error')}"}
    out["audio"] = {k: ma.get(k) for k in ("bgm_ducked", "sfx_count")}

    # 6) mux + normalize(记录 post_production)
    mm = mux_audio_video(str(work / "subtitled.mp4"), str(work / "soundbed.wav"),
                         str(work / "final.mp4"))
    if not mm.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"mux: {mm}"}
    nm = normalize_loudness(str(work / "final.mp4"), str(work / "final_norm.mp4"),
                            target_lufs=-14.0, two_pass=True)
    if not nm.get("ok"):
        return {"ok": False, "phase": "assemble", "reason": f"normalize: {nm}"}
    (work / "final_norm.mp4").replace(work / "final.mp4")
    import hashlib
    h = hashlib.sha256((work / "final.mp4").read_bytes()).hexdigest()
    store.record_artifact(project_id, "post_production", h)
    lm = loudness_measure(str(work / "final.mp4"))
    out["lufs"] = lm.get("input_i")

    # 7) 终验 + 发布(品牌/口号从 brief 与落版镜台词派生,不再写死"XX咖啡")
    last_narr = ""
    if shots:
        last_narr = str(shots[-1].get("narration") or "").strip()
    brand_name = _resolve_brand(brief)
    slogan = (str(brief.get("slogan") or "").strip() or last_narr
              or "享受每一刻")[:40]
    ctx = {"product_info": str(brief.get("product_info", "")),
           "brand_name": brand_name, "slogan": slogan,
           # 轮17:主角锚定(与 text 阶段同一解析式)——剧本钉了服装发型
           # 式样时,终审把跨镜换装从 warning 升 critical(违反剧本)
           "actor_anchor": (str(brief.get("actor_anchor")
                                or brief.get("hero_anchor")
                                or brief.get("character") or "").strip()),
           "duration_sec": round(sum(windows), 2),
           # G4(2026-09-21 审计):终验 VLM 逐帧对照分镜预期(场景/主体/动作),
           # 缺这些字段时「画面演错剧本」在审查里无从谈起
           "shots": [{"shot_id": s["shot_id"], "duration_sec": t["window_sec"],
                      "subject": str(s.get("subject"))[:40],
                      "scene": str(s.get("scene"))[:48],
                      "motion": str(s.get("motion"))[:40],
                      "narration": str(s.get("narration") or "")[:40]}
                     for s, t in zip(shots, tl)]}
    from shipin_platform.review.hard_gates import (vlm_review_final,
                                                   check_timeline,
                                                   check_narration_presence)
    # M7(2026-09-21 审计):成片层的素材复用/时序红线在此自动挂载为硬门,
    # 不再是工厂手动 API 端点。timeline 按 align 窗口构造:每镜一条,
    # at=该镜绝对开始时间、end=start+window,dur=window_sec。
    tl_entries = []
    _t_cursor = 0.0
    for s, t in zip(sids, tl):
        _dur = float(t.get("window_sec") or 0)
        # 与拼接步骤同一回退（_clip_src）：manifest 未记 clip 时用约定命名，
        # 否则时间轴会把缺失字段算成全员复用空串，误杀 REUSE 红线
        _clip = _clip_src(manifest, s, work)
        tl_entries.append({
            "shot_id": s,
            "src": _clip,
            "start": round(_t_cursor, 3),
            "end": round(_t_cursor + _dur, 3),
            "at": round(float(t.get("narr_at") or t.get("audio_start_sec")
                              or _t_cursor), 3)})
        _t_cursor += _dur
    # 审计 C2:剧本镜号全集 vs 时间线对账——漏镜/插镜由确定性门拦截
    tchk = check_timeline(tl_entries, duration_sec=a_total(tl),
                          expected_shot_ids=sids)
    _save(project_id, "timeline_check.json", tchk)
    out["timeline_check"] = {"verdict": tchk.get("verdict"),
                             "findings": tchk.get("findings", [])}
    fv = vlm_review_final(str(work / "final.mp4"), frames_count=16, context=ctx)
    # 轮14:每镜旁白声轨存在性(确定性,ASR-free)——TTS 失败/音频错位时
    # 画面照演但嘴上没词,「符不符合剧本」此前只核视频,音频侧在此补门
    _narr_shots = [{"shot_id": s["shot_id"],
                    "narration": s.get("narration"),
                    # 轮20:纯台词镜(narration 空)也要过声轨存在性门
                    "dialogue": s.get("dialogue"),
                    "narr_at": (t.get("narr_at") if isinstance(t, dict)
                                else None),
                    "audio_start_sec": (t.get("audio_start_sec")
                                        if isinstance(t, dict) else None),
                    "tts_sec": (t.get("tts_sec") if isinstance(t, dict)
                                else None),
                    "duration_sec": (t.get("window_sec") if isinstance(t, dict)
                                     else None)}
                   for s, t in zip(shots, tl)]
    nchk = check_narration_presence(str(work / "final.mp4"), _narr_shots)
    _save(project_id, "narration_check.json", nchk)
    out["narration_check"] = {"verdict": nchk.get("verdict"),
                              "findings": nchk.get("findings", [])}
    if tchk.get("verdict") == "fix":
        # timeline 红线(fix)直接并入终验——不能只靠 VLM 软提示
        fv = dict(fv)
        fv["findings"] = list(fv.get("findings") or []) + list(
            tchk.get("findings") or [])
        fv["verdict"] = "fix"
        fv["reason"] = (fv.get("reason") or "") + "；timeline 门未过"
    # 轮12:单镜 VLM 诊断(每镜独立 ctx 逐帧对照分镜预期)的 critical 并入
    # 终验——clip 自身演错剧本/镜内换人在此统一阻断,不随 assemble 的
    # master 补帧溜进终片。缺文件(旧项目/未跑 generate)时无操作。
    # 轮13:master 补料 part 的实际入拼片段复审(parts_review.json)同一范式。
    # 轮14:旁白声轨缺失(narration_check.json)同一范式。
    _sr_all = _load(project_id, "shots_review.json") or {}
    _pr_all = _load(project_id, "parts_review.json") or {}
    _nc = _load(project_id, "narration_check.json") or {}
    _sr_crit = [f for _r in list(_sr_all.values()) + list(_pr_all.values())
                + [_nc]
                for f in (_r.get("findings") or [])
                if f.get("severity") == "critical"]
    if _sr_crit:
        fv = dict(fv)
        fv["findings"] = list(fv.get("findings") or []) + _sr_crit
        fv["verdict"] = "fix"
        fv["reason"] = ((fv.get("reason") or "")
                        + f"；附属门(单镜诊断/入拼复审/旁白声轨) "
                          f"{len(_sr_crit)} 处 critical")
    # 轮23:通道级结论汇总——终审只说"按 findings 修复"不够,运营需要
    # 定位到「哪一镜的哪个通道」出的问题(clip 诊断/关键帧审图/入拼
    # 复审/旁白声轨),明细在各 *_review.json / *_check.json。
    def _ncrit(r: dict) -> int:
        return sum(1 for f in (r.get("findings") or [])
                   if f.get("severity") == "critical")

    _channels: list[dict] = []
    for _sid, _r in sorted((_sr_all or {}).items()):
        _is_kf = str(_sid).startswith("__")
        _channels.append({
            "channel": "keyframe" if _is_kf else "shot_review",
            "shot_id": str(_sid).strip("_").removeprefix("keyframe_"),
            "verdict": _r.get("verdict"), "critical": _ncrit(_r)})
    for _idx, _r in sorted((_pr_all or {}).items()):
        _channels.append({"channel": "part_review",
                          "shot_id": _r.get("shot_id"),
                          "part_idx": _idx, "verdict": _r.get("verdict"),
                          "critical": _ncrit(_r)})
    _channels.append({"channel": "narration", "verdict": _nc.get("verdict"),
                      "critical": _ncrit(_nc)})
    out["review_channels"] = _channels
    _save(project_id, "final_review.json", fv)
    out["final_review"] = {"verdict": fv.get("verdict"),
                           "deterministic": fv.get("deterministic"),
                           "brand_seen": fv.get("brand_seen"), "breaks": fv.get("breaks")}
    if fv.get("verdict") != "pass":
        return {"ok": True, "phase": "assemble", "released": False,
                "reason": "终验未通过,按 findings 修复后重跑 assemble", **out}
    # 发布:post_production 阶段已在第 6 步用成片内容哈希记 PASS(finalize 的
    # required 检查要求 status=PASS)——这里只留发布事件,不再覆盖成 RELEASED
    # 状态(此前把 hash 覆盖成字面量"RELEASED"导致 finalize 永远 409)。
    store.record_event(project_id, "released", "成片已发布(终验通过)",
                       stage="post_production",
                       detail=str(work / "final.mp4"))
    out["released"] = True
    out["final_path"] = str(work / "final.mp4")
    return {"ok": True, "phase": "assemble", **out}


def a_total(tl: list[dict]) -> float:
    return round(sum(t["window_sec"] for t in tl), 2)


def _clip_src(manifest: dict, shot_id: str, work: Path) -> str:
    """镜头源文件：manifest 记了 clip 用其路径，否则回退约定命名。

    拼接与时间轴红线共用同一回退——旧项目 manifest 缺 clip 字段时，
    若时间轴各自为政会把它算成「全员复用空串」，误杀 REUSE 红线。
    """
    rec = (manifest.get("shots") or {}).get(shot_id) or {}
    p = rec.get("clip")
    if p and str(p).strip():
        return str(p)
    return str(work / f"{shot_id}_clip.mp4")


def _build_srt(storyboard: dict, tl: list[dict], manifest: Optional[dict] = None) -> str:
    """SRT：旁白 + 台词双轨字幕。

    台词镜：台词「角色: 文本」先说（窗口起点起，显示时长 = 台词实长+0.1s），
    旁白随后（align 算好的 narr_at 起）。无台词镜只出旁白。
    """
    narr = {s["shot_id"]: s.get("narration", "") for s in storyboard["shots"]}
    dlg = {s["shot_id"]: s.get("dialogue") for s in storyboard["shots"]}
    ROLE_NAMES = {"hero_male": "主角", "colleague_male": "同事",
                  "assistant_female": "助理", "biz_female": "旁白"}

    def fmt(t):
        h, m = int(t // 3600), int(t % 3600 // 60)
        sec, ms = int(t % 60), int(round((t % 1) * 1000))
        return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"
    lines, t = [], 0.0
    idx = 1
    for rec in tl:
        w = rec["window_sec"]
        d = dlg.get(rec["shot_id"])
        n = narr.get(rec["shot_id"], "")
        if isinstance(d, dict) and d.get("text"):
            role = str(d.get("role_code") or "biz_female")
            name = ROLE_NAMES.get(role, role)
            dlg_sec = rec.get("dlg_sec") or 1.2
            d_show = min(dlg_sec + 0.1, w)
            lines.append(f"{idx}\n{fmt(t)} --> {fmt(t + d_show)}\n{name}: {d['text']}\n")
            idx += 1
            if n:
                ns = t + dlg_sec + 0.18
                if ns < t + w - 0.05:
                    lines.append(f"{idx}\n{fmt(ns)} --> {fmt(t + w)}\n{n}\n")
                    idx += 1
        elif n:
            lines.append(f"{idx}\n{fmt(t)} --> {fmt(t + w)}\n{n}\n")
            idx += 1
        t += w
    return "\n".join(lines)
