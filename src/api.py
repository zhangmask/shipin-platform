"""Shipin Platform API — FastAPI service wrapping shipin-platform + OpenMontage tools."""
from __future__ import annotations

import copy
import ipaddress
import json as _json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Optional

# 源码模式：先把 src/ 与可选 OpenMontage 放入 path，再导入平台包；
# 打包模式：全部模块已在 bundle 内，无需注入（roots 决定数据根）。
if not getattr(sys, "frozen", False):
    _src_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(_src_root))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent
                           / "OpenMontage-main" / "OpenMontage-main"))

from shipin_platform import roots as _roots

_shipin_root = _roots.data_root()  # 数据根：源码=项目根；打包=exe 旁 shipin-data

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv(_shipin_root / ".env")

from shipin_platform import load_config, ReviewEngine
from shipin_platform.tools.ffmpeg_engine import FFmpegEngine, OM_TOOLS_AVAILABLE

# WhisperService 依赖可选的 openai-whisper；缺包时只影响 /api/subtitle/*，
# 不再阻断整个 API 的导入（与 README“whisper 可选”一致）。
try:
    from shipin_platform.tools import WhisperService  # noqa: F401
except ImportError:  # openai-whisper 未安装
    WhisperService = None
from shipin_platform.tools.subtitle_renderer import (
    build_ass_style, generate_srt, hex_to_ass_color, escape_subtitles_path,
    render_subtitles_best,
)
from shipin_platform.review import score_slideshow_risk, check_scene_variation
from shipin_platform.review.clip_qc import qc_clip
from shipin_platform.review.llm_review import llm_stage_review
from shipin_platform.assembly import (
    align_narration, build_transition_stitch, master_audio, kenburns,
    XFADE_TRANSITIONS,
)
from shipin_platform.orchestration.pipeline_runner import (
    run_text_phase, run_generate_phase, run_assemble_phase,
)
from shipin_platform.services.costing import (
    record_cost, cost_summary, global_cost_summary, global_budget_exceeded,
    read_global_budget, set_global_budget, LedgerCorruptError)
from shipin_platform.services.rate_limiter import (
    check_rate, rate_state, rate_config)
from shipin_platform.services import audit_store

# Agent 受控执行入口（HTTP 层同样受白名单/顺序/审查门约束）；app 定义后再注册
from shipin_platform.guard.http_api import register_guard_endpoints

# Agent 受控执行入口（HTTP 层同样受白名单/顺序/审查门约束）
from shipin_platform.guard.http_api import register_guard_endpoints
from shipin_platform.orchestration import ProjectStageStore, StageGateError, STAGES
from shipin_platform.guard.api_auth import (
    ApiKeyStore, Principal, auth_mode, is_public_path,
    principal_from_header, project_allowed,
)
from shipin_platform.generation.generate_assets import (
    generate_image_pil, generate_image_flux, generate_image_openai,
    generate_image_agnes, generate_video, generate_video_agnes,
)
from shipin_platform.services.tts_service import create_tts_service

# ── SSRF 防护（与 hard_gates 同模式）────────────────────────────
_IMG_HOSTS = {"images.unsplash.com", "cdn.openai.com", "openai-api.img",
              "platform-outputs.agnes-ai.space"}


def _safe_fetch(url: str, timeout: int = 30) -> bytes:
    """Fetch URL with protocol/host/IP allowlist.  Blocks private/loopback."""
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme != "https":
        raise ValueError("only https allowed")
    if u.hostname not in _IMG_HOSTS:
        raise ValueError(f"host {u.hostname} not in allowlist")
    for info in socket.getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("blocked IP")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": "shipin-platform/1.0"})
    with opener.open(req, timeout=timeout) as r:
        return r.read()

# 目录初始化：打包首启复制默认配置到可写区；源码模式 no-op
_roots.ensure_config_dir()
_roots.ensure_data_dirs()

app = FastAPI(title="Shipin Platform API", version="0.6.0")

# guard 端点挂载（必须在 app 定义之后）
register_guard_endpoints(app)

# ── P7 CORS 白名单（OWASP API8：默认全关，仅同源；显式白名单才放行）──
# 逗号分隔环境变量 SHIPIN_CORS_ORIGINS=https://a.example,https://b.example
def _cors_origins_from_env():
    raw = os.environ.get("SHIPIN_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins_from_env(),  # 空=仅同源；永不接受 "*"
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["X-API-Key", "Content-Type", "Authorization"],
    allow_credentials=False,
)

# ── P0 平台级鉴权（X-API-Key；SHIPIN_AUTH_MODE=off 时放行）────────────
# 数据文件与事件台账同目录；keys 只存 sha256 指纹，明文仅签发时可见一次。
from fastapi import Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

_AUTH_STORE: Optional[ApiKeyStore] = None


def _auth_store() -> ApiKeyStore:
    global _AUTH_STORE
    if _AUTH_STORE is None:
        _AUTH_STORE = ApiKeyStore(
            str(_shipin_root / "data" / "auth_keys.db"))
    return _AUTH_STORE


@app.middleware("http")
async def _platform_auth(request: Request, call_next):
    """前置管道：P7 限流 → P0 鉴权 → P7 审计。拒绝走 JSON 401/403/429。

    - 仅对 /api/*、非公开、非 OPTIONS 的请求计数（token 指纹或 IP 兜底）
    - 每个 /api 请求落一条 request_audit（含拒绝），满足 OWASP API10
    """
    path = request.url.path
    is_api = path.startswith("/api/")
    public = is_public_path(path)
    client_ip = request.client.host if request.client else "unknown"

    # CORS 预检（OPTIONS）旁路鉴权/限流：浏览器跨源请求必须先收到
    # 200 预检应答才会携带 X-API-Key 发起真实请求（OWASP API8）。
    # 计数侧也排除，避免预检刷桶。
    if request.method == "OPTIONS":
        resp = await call_next(request)
        if is_api:
            audit_store.record(caller="anonymous", ip=client_ip,
                               method=request.method, route=path,
                               status=resp.status_code, kind="preflight")
        return resp

    if is_api and not public:
        allowed, info = check_rate(
            token=request.headers.get("X-API-Key"), ip=client_ip)
        if not allowed:
            audit_store.record(caller="anonymous", ip=client_ip,
                               method=request.method, route=path, status=429,
                               kind="rate_limited",
                               detail=f"limit={info['limit']} "
                                      f"retry_after={info['retry_after']}")
            return JSONResponse(status_code=429, headers={
                "X-RateLimit-Limit": str(info["limit"]),
                "X-RateLimit-Remaining": str(info["remaining"]),
                "X-RateLimit-Window": str(info["window"]),
                "Retry-After": str(info["retry_after"])},
                content={"detail": f"[RATE_LIMITED] exceed {info['limit']} "
                         f"requests / {info['window']}s window"})

    if public or auth_mode() != "strict":
        resp = await call_next(request)
        if is_api:
            audit_store.record(caller="anonymous", ip=client_ip,
                               method=request.method, route=path,
                               status=resp.status_code, kind="request")
        return resp

    principal = principal_from_header(dict(request.headers), _auth_store())
    if principal is None:
        audit_store.record(caller="anonymous", ip=client_ip,
                           method=request.method, route=path, status=401)
        return JSONResponse(status_code=401, content={
            "detail": "[UNAUTHENTICATED] missing or invalid X-API-Key; "
                      "admin: set SHIPIN_ADMIN_KEY"})
    if path.startswith("/api/platform"):
        if not principal.is_admin:
            audit_store.record(caller=principal.caller, ip=client_ip,
                               method=request.method, route=path, status=403,
                               scope=principal.scope,
                               detail="admin required for /api/platform")
            return JSONResponse(status_code=403, content={
                "detail": "[FORBIDDEN_SCOPE] admin required for /api/platform"})
    elif request.method not in ("GET", "HEAD", "OPTIONS"):
        if not principal.can_write:
            audit_store.record(caller=principal.caller, ip=client_ip,
                               method=request.method, route=path, status=403,
                               scope=principal.scope, detail="scope cannot write")
            return JSONResponse(status_code=403, content={
                "detail": f"[FORBIDDEN_SCOPE] key scope='{principal.scope}' "
                          "cannot write"})
    # body 型端点（project_id 在 JSON body）不在路径里 —— 路径中的
    # "create"/"text" 等词不能误当 project_id；这些由端点内
    # _enforce_project_binding 检查。
    _BODY_PROJECT_ENDPOINTS = (
        "/api/project/create", "/api/project/confirm",
        "/api/pipeline/text", "/api/pipeline/generate",
        "/api/pipeline/assemble",
    )
    if not path.startswith(_BODY_PROJECT_ENDPOINTS):
        m = re.match(r"^/api/(?:pipeline|project|variant)/([^/]+)",
                     request.url.path)
        if m and not project_allowed(principal, m.group(1)):
            audit_store.record(caller=principal.caller, ip=client_ip,
                               method=request.method, route=path, status=403,
                               scope=principal.scope,
                               kind="project_denied", detail=m.group(1))
            return JSONResponse(status_code=403, content={
                "detail": f"[FORBIDDEN_PROJECT] key bound to "
                          f"'{principal.project_id}' cannot access "
                          f"'{m.group(1)}'"})
    request.state.principal = principal
    resp = await call_next(request)
    if is_api:
        audit_store.record(caller=principal.caller, ip=client_ip,
                           method=request.method, route=path,
                           status=resp.status_code, scope=principal.scope)
    return resp

# ── Web 前端（React 构建产物，/ui 同源挂载；未构建时优雅跳过）──────
_UI_DIST = _roots.resource_root() / "web" / "dist"
if _UI_DIST.is_dir():
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    app.mount("/ui", StaticFiles(directory=str(_UI_DIST), html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def _root():
        return RedirectResponse("/ui/")
else:
    print(f"[api] 未发现 {_UI_DIST}，跳过 /ui 挂载（先 cd web && npm run build）")

# ── Project state machine (§11) ───────────────────────────────────
# One SQLite-backed store for every project's stage lineage.  Downstream
# operations that carry a project_id are hard-blocked unless the upstream
# stage is PASS with a matching artifact hash.

_STAGE_STORE: Optional[ProjectStageStore] = None


def _stage_store() -> ProjectStageStore:
    global _STAGE_STORE
    if _STAGE_STORE is None:
        _STAGE_STORE = ProjectStageStore(
            str(_shipin_root / "data" / "stage_store.db"))
    return _STAGE_STORE


_TASKS_STORE = None


def _task_store():
    """P1 异步任务存储（SQLite 任务表 + 线程池）；惰性单例。"""
    global _TASKS_STORE
    if _TASKS_STORE is None:
        from shipin_platform.services.task_store import TaskStore
        _TASKS_STORE = TaskStore(
            db_path=str(_shipin_root / "data" / "tasks.db"),
            stage_db_path=str(_shipin_root / "data" / "stage_store.db"))
    return _TASKS_STORE


def _enforce_task_binding(request: Request, task: dict) -> None:
    """任务归属隔离：绑定项目 key 只能读/重试自己项目的任务。"""
    principal = getattr(getattr(request, "state", None), "principal", None)
    if principal is None or principal.is_admin:
        return
    if principal.project_id and task.get("project_id") != principal.project_id:
        raise HTTPException(
            status_code=403,
            detail=f"[FORBIDDEN_PROJECT] api key bound to "
                   f"'{principal.project_id}' cannot access task of "
                   f"'{task.get('project_id')}'")


def _enqueue_phase(request: Request, kind: str, project_id: str):
    """后台任务入队：202 + task_id。预算硬闸在入队前同步执行（C5 语义不变），
    phase 事件由 worker 记录；同步路径完全保留（MCP/既有测试不变）。"""
    store = _stage_store()
    _enforce_budget(project_id, store)
    ts = _task_store()
    ts.recover_stale()
    principal = getattr(getattr(request, "state", None), "principal", None)
    caller = principal.caller if principal is not None else "anonymous"

    def _run() -> dict:
        if kind == "generate":
            store.record_event(project_id, "phase_started",
                               "阶段二 generate 启动（异步任务）",
                               stage="video_gen")
            r = run_generate_phase(project_id, store)
            store.record_event(
                project_id, "phase_finished",
                f"阶段二 generate 结束：{'ok' if r.get('ok') else 'failed'}",
                stage="video_gen",
                detail=str(r.get("reason") or
                           f"{len(r.get('report') or [])} 镜"))
        else:
            store.record_event(project_id, "phase_started",
                               "阶段三 assemble 启动（异步任务）",
                               stage="post_production")
            r = run_assemble_phase(project_id, store)
            store.record_event(
                project_id, "phase_finished",
                f"阶段三 assemble 结束：{'released' if r.get('released') else 'failed'}",
                stage="post_production",
                detail=str(r.get("reason") or ""))
            r["preview_frames"] = _refresh_preview_frames(project_id)
        return r

    task = ts.submit_task(kind, project_id, caller, _run)
    return JSONResponse(status_code=202, content=task)


def _enforce_project_binding(request: Request, project_id: str) -> None:
    """绑定项目 key 只能访问它绑定的项目（body/路径都走这里的统一点）。
    未绑定/管理员不做限制。"""
    principal = getattr(request.state, "principal", None)
    if principal is None:
        return
    if principal.project_id is not None and principal.project_id != project_id:
        raise HTTPException(
            status_code=403,
            detail=f"[FORBIDDEN_PROJECT] api key bound to "
                   f"'{principal.project_id}' cannot access '{project_id}'")


class ProjectCreateRequest(BaseModel):
    project_id: str


@app.post("/api/project/create")
def project_create(req: ProjectCreateRequest, request: Request):
    store = _stage_store()
    _enforce_project_binding(request, req.project_id)
    # owner：来自调用方身份（无鉴权/离线时缺省 default）
    owner = getattr(request.state, "principal", None)
    owner = (owner.caller if owner else None) or "default"
    try:
        store.create_project(req.project_id, owner=owner)
    except StageGateError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "project_id": req.project_id,
            "owner": owner, "stages": STAGES}


@app.get("/api/project/{project_id}/status")
def project_status(project_id: str):
    """项目状态全景（P4 起为外部 AI 富化决策上下文）：
    原字段 stages/confirmations/clip_qc 之上新增 gates_pending（待确认闸门）、
    artifacts（产物清单含版本数）、budget（上限/已用/是否超限）、
    recent_events（近期轨迹摘要）。AI 依此决定下一步，无需再拼多个端点。"""
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    confirmations = {g: store.get_confirmation(project_id, g)
                     for g in ("brief", "script", "storyboard")}
    gates_pending = [g for g in ("brief", "script", "storyboard")
                     if confirmations[g] is None]
    from shipin_platform.services.artifact_store import all_versions
    vindex = all_versions(_project_dir(project_id))
    artifacts = []
    for stage, fname in _ARTIFACT_FILES.items():
        fp = _project_dir(project_id) / fname
        arts = vindex.get(stage) or []
        artifacts.append({
            "stage": stage, "file": fname, "exists": fp.is_file(),
            "bytes": fp.stat().st_size if fp.is_file() else 0,
            "versions": len(arts),
            "head_version": max((e["version"] for e in arts), default=None),
        })
    budget_cfg = _read_budget(project_id)
    used = cost_summary(project_id)["total_usd"]
    budget = {
        "max_budget_usd": budget_cfg["max_budget_usd"] if budget_cfg else None,
        "set_at": budget_cfg["set_at"] if budget_cfg else None,
        "current_usd": used,
        "exceeded": bool(budget_cfg
                         and budget_cfg["max_budget_usd"] is not None
                         and used > budget_cfg["max_budget_usd"]),
    }
    return {"project_id": project_id,
            "stages": store.get_project_status(project_id),
            "confirmations": confirmations,
            "gates_pending": gates_pending,
            "artifacts": artifacts,
            "budget": budget,
            "recent_events": store.list_events(project_id, limit=8),
            "clip_qc": store.list_clip_qc(project_id)}


class ConfirmRequest(BaseModel):
    project_id: str
    gate: str                          # brief / script / storyboard
    approved_by: str = "user"
    note: str = ""


@app.post("/api/project/confirm")
def project_confirm(req: ConfirmRequest, request: Request):
    """人工确认闸门。工作流规定：agent 必须先把 brief 卡/剧本表/分镜表展示给
    用户、得到明确确认后才能调用本端点；生成类端点会硬性校验这里的状态。
    approved_by 必须如实填写（'user' 或代确认人），审计可查。"""
    store = _stage_store()
    _enforce_project_binding(request, req.project_id)
    try:
        result = store.record_confirmation(req.project_id, req.gate,
                                           approved_by=req.approved_by,
                                           note=req.note)
    except StageGateError as e:
        raise _gate_error_response(e)
    next_map = {
        "brief": "next: POST /api/review/iterate (stage=script)，生成剧本",
        "script": "next: 展示剧本摘要→确认后 POST /api/review/iterate (stage=storyboard)",
        "storyboard": "next: /api/generate/image 逐镜首帧 → /api/generate/agnes-video (带 first_frame)",
    }
    return {**result, "next_action": next_map.get(req.gate, "")}


class AlignRequest(BaseModel):
    project_id: Optional[str] = None
    shots: list[dict]   # [{shot_id, duration_sec, narration_path}]
    min_tail: float = 0.25
    max_gap: float = 0.8
    master_duration: float = 10.0


@app.post("/api/audio/align")
def audio_align(req: AlignRequest):
    """旁白-镜头对齐门（确定性）：按旁白实长计算每镜最终窗口。
    coffee-v5 病根修复：旁白按固定秒数铺、S06 溢出镜头、S05/S07 干晾 1s+。
    verdict=fix（旁白比素材还长）时禁止拼接；ok 时把返回 timeline 的
    window_sec 传给 /api/video/stitch、audio_start_sec 传给 /api/audio/master。"""
    report = align_narration(req.shots, min_tail=req.min_tail,
                             max_gap=req.max_gap,
                             master_duration=req.master_duration)
    return report


class MasterAudioRequest(BaseModel):
    narration_path: Optional[str] = None   # 已拼好的旁白床（与 narration_events 二选一）
    narration_events: Optional[list[dict]] = None  # [{path, time, gain_db}] 逐镜旁白定位（平台内部混音）
    duration_sec: float        # 成片总长（= align 的 total_sec）
    output: str
    bgm_path: Optional[str] = None
    bgm_gain_db: float = -19.0
    duck: bool = True
    sfx_events: Optional[list[dict]] = None  # [{time, kind|path, gain_db}]
    project_id: Optional[str] = None


@app.post("/api/audio/master")
def audio_master(req: MasterAudioRequest):
    """声音设计主混音：旁白（逐镜定位或成品床）+ 自动闪避 BGM + 切点音效
    （whoosh/pop 可合成）。说话时音乐退后、气口时音乐浮上来、切换点有风声划过。"""
    result = master_audio(
        narration_path=req.narration_path,
        narration_events=req.narration_events,
        duration_sec=req.duration_sec,
        output=req.output, bgm_path=req.bgm_path,
        bgm_gain_db=req.bgm_gain_db, duck=req.duck,
        sfx_events=req.sfx_events)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error"))
    return result


class KenBurnsRequest(BaseModel):
    image_path: str
    duration: float
    output: str
    zoom_to: float = 1.10
    fps: int = 24
    size: str = "1280x704"


@app.post("/api/video/kenburns")
def video_kenburns(req: KenBurnsRequest):
    """静态图 → 缓慢推近视频（品牌落版/静态卡专用，替代死板定格）。"""
    result = kenburns(image_path=req.image_path, duration=req.duration,
                      output=req.output, zoom_to=req.zoom_to, fps=req.fps,
                      size=req.size)
    if not result.get("ok"):
        raise HTTPException(status_code=500, detail=result.get("error"))
    return result


class QcClipRequest(BaseModel):
    clip_path: str
    shot_id: str = ""
    project_id: Optional[str] = None
    expected_duration_sec: Optional[float] = None
    duration_tolerance: float = 0.7
    reference_image: Optional[str] = None   # 首帧参考图（分镜生图）
    scene_threshold: float = 0.3
    max_internal_cuts: int = 0
    check_motion: bool = True
    motion_floor: float = 1.0               # 品牌落版等静态镜显式传 0
    expected_resolution: Optional[list] = None
    use_vlm: bool = False


@app.post("/api/qc/clip")
def qc_clip_endpoint(req: QcClipRequest):
    """每镜头硬门（确定性，无模型依赖）：
    时长匹配 / 镜内硬切=0 / 运动能量下限 / 首帧 vs 参考图感知距离。
    verdict=fix 的镜头禁止进入拼接（stitch 端点按本表硬拦）。
    brand 落版等设计上静态的镜头：check_motion=false 或 motion_floor=0。"""
    report = qc_clip(
        clip_path=req.clip_path,
        shot_id=req.shot_id,
        expected_duration_sec=req.expected_duration_sec,
        duration_tolerance=req.duration_tolerance,
        reference_image=req.reference_image,
        scene_threshold=req.scene_threshold,
        max_internal_cuts=req.max_internal_cuts,
        check_motion=req.check_motion,
        motion_floor=req.motion_floor,
        expected_resolution=req.expected_resolution,
        use_vlm=req.use_vlm,
    )
    if req.project_id and req.shot_id:
        store = _stage_store()
        try:
            store.create_project(req.project_id)
        except StageGateError as e:
            raise _gate_error_response(e)
        store.record_clip_qc(req.project_id, req.shot_id, req.clip_path,
                             report["verdict"], report)
    return report


def _gate_error_response(e: StageGateError):
    return HTTPException(status_code=409, detail={"code": e.code, "message": str(e)})


# ── Request Models ───────────────────────────────────────────────

class BriefReviewRequest(BaseModel):
    brief: dict
    round: int = 1


class ScriptReviewRequest(BaseModel):
    script: dict
    round: int = 1


class StoryboardReviewRequest(BaseModel):
    storyboard: dict
    round: int = 1


class ImagePromptReviewRequest(BaseModel):
    prompts: dict
    round: int = 1


class VideoPromptReviewRequest(BaseModel):
    prompts: dict
    round: int = 1


class SubtitleRequest(BaseModel):
    audio_path: str
    output_dir: str = "./outputs"
    model: str = "base"
    language: str = "zh"


class BurnSubtitleRequest(BaseModel):
    video_path: str
    srt_path: str
    output_path: str
    font_name: str = "Microsoft YaHei"
    # 平台合规默认值（AGENT_GUIDE §10.1）：字号 ≤ 5% 屏高，底部安全区 ≥ 96px
    font_size: int = 46
    alignment: int = 2
    margin_v: int = 96
    font_path: Optional[str] = None    # 显式指定 drawtext 字体（可选）
    mode: str = "auto"                 # auto / drawtext / subtitle
    project_id: Optional[str] = None   # 非空时触发下游状态门


class StitchRequest(BaseModel):
    clips: list[str]
    output: str
    transition: str = "cut"          # cut / crossfade / fade / dissolve / smoothleft / circleopen ...
    transition_duration: float = 0.4
    project_id: Optional[str] = None   # 非空时触发下游状态门
    # ── v2：旁白驱动 + 保时长转场 ──────────────────────────────────
    windows: Optional[list[float]] = None  # 每镜最终窗口（来自 /api/audio/align）
    masters: Optional[list[str]] = None    # 未裁剪 master（转场借帧用）
    boundary_transitions: Optional[list[str]] = None  # 逐边界转场（chain=cut/跳变=dissolve）


class DuckRequest(BaseModel):
    primary_audio: str
    secondary_audio: str
    output_path: str
    duck_level: float = -12.0


class MixRequest(BaseModel):
    tracks: list[dict]
    output_path: str
    normalize: bool = True


class MediaPathRequest(BaseModel):
    path: str


class ScenesRequest(BaseModel):
    scenes: list[dict]


class TimelineReviewRequest(BaseModel):
    timeline: object          # 接受 dict（含 timeline 字段）或 list（裸片段数组）
    duration_sec: Optional[float] = None


class FinalVideoReviewRequest(BaseModel):
    video_path: str
    frames: int = 12
    # 项目上下文：product_info/brand_name/duration_sec/shots[{shot_id,duration_sec,subject}]
    # 传入后终验按分镜表抽帧 + 确定性镜内切检查；不传则退化为中性通用检查
    context: dict = {}
    # 轮30:可选 project_id——手工/agent 链路(stitch→burn/mux→终验→finalize)
    # 的终审凭证出口。传入且 verdict=pass 时把结论落盘 final_review.json,
    # finalize 的终审闸(轮25)才认;fix/blocked 不落盘,闸的严格性不变。
    project_id: str = ""


class EncodeRequest(BaseModel):
    input_path: str
    output_path: str
    crf: int = 20
    preset: str = "veryfast"
    profile: str = "high"


class ColorGradeRequest(BaseModel):
    video_path: str
    output_path: str
    preset: str = "warm_tvc"  # warm_tvc / neutral / bright_social


# ── Review Endpoints ─────────────────────────────────────────────

@app.post("/api/review/brief")
def review_brief(req: BriefReviewRequest):
    engine = ReviewEngine()
    report = engine.run_review("brief", req.brief, round_num=req.round)
    return report.to_dict()


@app.post("/api/review/script")
def review_script(req: ScriptReviewRequest):
    engine = ReviewEngine()
    report = engine.run_review("script", req.script, round_num=req.round)
    return report.to_dict()


@app.post("/api/review/storyboard")
def review_storyboard(req: StoryboardReviewRequest):
    engine = ReviewEngine()
    report = engine.run_review("storyboard", req.storyboard, round_num=req.round)
    scenes = req.storyboard.get("scenes", [])
    result = report.to_dict()
    result["slideshow_risk"] = score_slideshow_risk(scenes)
    result["variation_check"] = check_scene_variation(scenes)
    return result


@app.post("/api/review/image-prompt")
def review_image_prompt(req: ImagePromptReviewRequest):
    engine = ReviewEngine()
    report = engine.run_review("image_prompt", req.prompts, round_num=req.round)
    return report.to_dict()


@app.post("/api/review/video-prompt")
def review_video_prompt(req: VideoPromptReviewRequest):
    engine = ReviewEngine()
    report = engine.run_review("video_prompt", req.prompts, round_num=req.round)
    return report.to_dict()


class IterateRequest(BaseModel):
    stage: str          # brief / script / storyboard / image_prompt / video_prompt
    data: dict
    max_rounds: int = 3
    project_id: Optional[str] = None   # 传入即接入状态机（§11）
    # 语义审查（固定 rubric 的 LLM 逻辑评审）：script/storyboard 可开启，
    # findings 与规则引擎合并出 decision；无 key 时自动跳过并在响应中注明
    llm_review: bool = False
    brief: Optional[dict] = None       # script 语义审查的上下文（产品/平台/基调）


@app.post("/api/review/iterate")
def review_iterate(req: IterateRequest):
    """Run the full multi-round review loop server-side: review → mechanical
    fix → re-review, until PASS / STALL / STOP. Returns every round's report
    plus the final (fixed) data and the failure modes that still need the
    caller (LLM/user) to regenerate.

    With ``project_id``: PASS results are recorded into the project state
    machine (artifact hash + downstream invalidation on rewrite), so any
    downstream call carrying the project_id can be hard-blocked when its
    upstream is stale or missing."""
    valid_stages = {"brief", "script", "storyboard", "image_prompt", "video_prompt"}
    if req.stage not in valid_stages:
        raise HTTPException(status_code=422, detail=f"stage must be one of {sorted(valid_stages)}")
    max_rounds = max(1, min(req.max_rounds, 10))

    store = None
    if req.project_id:
        store = _stage_store()
        try:
            store.create_project(req.project_id)
        except StageGateError as e:
            raise _gate_error_response(e)
        # 上游链硬化：上游阶段一旦存在且不是 PASS（BLOCKED/陈旧），禁止下游迭代，
        # 防止在脏数据上继续堆产物（v4 病根：video_gen 在 storyboard 改写后仍 PASS）。
        if req.stage != "brief":
            upstream = STAGES[STAGES.index(req.stage) - 1]
            up = store.get_stage(req.project_id, upstream)
            if up is not None and up["status"] != "PASS":
                raise HTTPException(status_code=409, detail={
                    "code": "UPSTREAM_STALE",
                    "message": (f"上游阶段 '{upstream}' 状态为 {up['status']}，"
                                f"禁止在陈旧产物上迭代 '{req.stage}'；"
                                f"先重新通过上游阶段"),
                    "upstream": upstream, "status": up["status"]})

    engine = ReviewEngine({"max_rounds": {req.stage: max_rounds}})
    data = copy.deepcopy(req.data)
    rounds: list[dict] = []
    prev_report = None
    fix_applied = False
    last_fix: Optional[dict] = None

    for round_num in range(1, max_rounds + 1):
        report = engine.run_review(
            req.stage, data, round_num=round_num,
            previous_report=prev_report, fix_applied=fix_applied,
        )
        rounds.append(report.to_dict())
        if report.decision.value in ("pass", "pass_with_warnings", "stall", "stop"):
            break
        last_fix = engine.revision.fix(req.stage, data, report)
        data = last_fix["data"]
        fix_applied = bool(last_fix["applied"])
        prev_report = report

    # ── 语义审查（固定 rubric，LLM）：与规则审查合并 ────────────────
    # 规则引擎只查结构，剧情逻辑/可拍性/身份一致查不出来——曾经五道关全
    # PASS、问题全漏到成片。llm_review=true 时对 script/storyboard 追加
    # 语义审查；critical 未清零则一律不放行。
    llm_meta: dict = {}
    if req.llm_review and req.stage in ("script", "storyboard"):
        llm = llm_stage_review(req.stage, data, brief=req.brief)
        if llm["available"]:
            final = rounds[-1]
            for f in llm["findings"]:
                cls = engine.classifier.classify(req.stage, f["issue"], f["evidence"])
                final["findings"].append({
                    "dimension": f"llm_{f['dimension']}",
                    "severity": "critical" if f["severity"] == "critical" else "suggestion",
                    "issue": f["issue"],
                    "evidence": f["evidence"],
                    "failure_mode": cls["mode"],
                    "revision_strategy": cls["strategy"],
                    "proposed_fix": f["fix"],
                    "status": "pending",
                })
            final["stats"]["critical"] = sum(
                1 for f in final["findings"] if f["severity"] == "critical")
            final["stats"]["suggestion"] = sum(
                1 for f in final["findings"] if f["severity"] == "suggestion")
            if final["stats"]["critical"] > 0 and final["decision"] in ("pass", "pass_with_warnings"):
                final["decision"] = "revise"
                final["next_action"] = "语义审查发现 critical 问题，按 revision_plan 修改后重新提交"
            final["metadata"]["llm_review"] = {
                "scores": llm.get("scores", {}),
                "finding_count": len(llm["findings"]),
            }
            final["revision_plan"] = [
                *(f"必改 · llm_{f['dimension']} · {f['issue']} → 怎么改：{f['proposed_fix']}"
                  for f in final["findings"]
                  if f["severity"] == "critical" and str(f["dimension"]).startswith("llm_")),
                *final.get("revision_plan", []),
            ]
        else:
            llm_meta = {"llm_review": "skipped", "reason": llm["reason"]}

    final = rounds[-1]
    blocked = final["decision"] in ("stall", "stop")

    # ── state machine integration (§11) ─────────────────────────────
    artifact_hash = None
    if store is not None:
        from shipin_platform.contracts import stable_artifact_hash
        artifact_hash = stable_artifact_hash(data)
        if final["decision"] in ("pass", "pass_with_warnings"):
            upstream = STAGES[STAGES.index(req.stage) - 1] if STAGES.index(req.stage) > 0 else None
            parent_hash = None
            if upstream:
                up = store.get_stage(req.project_id, upstream)
                if up and up.get("artifact_hash"):
                    parent_hash = up["artifact_hash"]
            store.record_artifact(req.project_id, req.stage, artifact_hash,
                                  parent_hash=parent_hash)
        else:
            store.record_artifact(req.project_id, req.stage, artifact_hash,
                                  status="BLOCKED")
        # ── 产物落盘 + 内容变更时下游失效（与 /rewrite 同语义）──────
        # 原来只记状态不写盘：AI 修复的 script/storyboard 落到响应里但不写
        # script.json/storyboard.json，下游 generate 读磁盘旧稿 → 状态机
        # PASS 与磁盘内容对不上，改了个寂寞。这里把迭代结果写回文件；哈希
        # 变化说明内容真变了 → 清确认 + 失效下游，杜绝「改完旧链条继续花钱」。
        if req.stage in ("script", "storyboard"):
            from shipin_platform.orchestration.pipeline_runner import (
                _save as _runner_save)
            prev = store.get_stage(req.project_id, req.stage)
            changed = prev is None or prev["artifact_hash"] != artifact_hash
            _runner_save(req.project_id, f"{req.stage}.json", data)
            if changed and final["decision"] in ("pass", "pass_with_warnings"):
                store.clear_confirmation(req.project_id, req.stage)
                n = store.invalidate_downstream(req.project_id, req.stage)
                store.record_event(req.project_id, "stage_rewritten",
                                   f"iterate 修正 {req.stage} 已写回，需重新确认",
                                   stage=req.stage,
                                   detail=f"下游 {n} 个阶段已失效")

    return {
        "stage": req.stage,
        "decision": final["decision"],
        "blocked": blocked,      # STALL/STOP = 硬阻断：禁止进入下一步/交付
        "blocked_reason": (
            "迭代未收敛（stall 或达到轮数上限 stop）：critical 项未清零，"
            "禁止交付；必须由 LLM 按 revision_plan/manual_modes 重写后再 iterate"
            if blocked else ""
        ),
        "rounds_run": len(rounds),
        "rounds": rounds,
        "final": final,
        "revision_plan": final.get("revision_plan", []),
        "data": data,
        "manual_modes": (last_fix or {}).get("manual", []),
        "project_id": req.project_id,
        "artifact_hash": artifact_hash,
        **({"llm": llm_meta} if llm_meta else {}),
    }


# ── 交付硬闸门（§10.7.1/§10.8）─────────────────────────────────
# 历史教训：规则只写在 AGENT_GUIDE，引擎不执行 = 审核形同虚设。
# 这两个 gate 是确定性校验 + VLM 视觉复审，成片必须双绿才能交付。

@app.post("/api/review/timeline")
def review_timeline(req: TimelineReviewRequest):
    """Deterministic timeline gate: same-source reuse (≤3, echoes ≥20s apart),
    monotonic order, coverage vs target duration. No LLM involved."""
    from shipin_platform.review.hard_gates import check_timeline
    return check_timeline(req.timeline, req.duration_sec)


@app.post("/api/review/final-video")
def review_final_video(req: FinalVideoReviewRequest, request: Request):
    """双层终验：确定性结构检查（镜内切/节奏）+ VLM 走查（context 参数化，
    按 project context 的分镜表抽帧）。Returns blocked (no AGNES_KEY),
    pass, or fix-with-findings."""
    from shipin_platform.review.hard_gates import vlm_review_final
    # 轮34:带 project_id(要落终审凭证)必须过请求级项目绑定——否则持有
    # 任绑到 A 的 write key 就能给任意项目 B 覆写 final_review.json
    # (毁证/污染发布态,四审审计 #3;verdict=pass 的落盘在绑定之后)。
    if req.project_id:
        _enforce_project_binding(request, req.project_id)
    # 轮33:带 project_id 时被审视频必须位于该项目目录内——否则调用方可
    # 对 preview cut/别的项目的视频跑终验,把 pass 凭证签给本项目
    # (三审审计 #1:凭证与"被审视频/项目"零绑定,finalize 只读 verdict)。
    if req.project_id:
        _p = Path(req.video_path).resolve()
        _proj = _project_dir(req.project_id).resolve()
        try:
            _p.relative_to(_proj)
        except ValueError:
            return {"verdict": "blocked",
                    "reason": (f"video_path 不在项目 {req.project_id} 目录内"
                               f"——拒绝对外部/他项目视频签发本项目终审凭证")}
    _res = vlm_review_final(req.video_path, frames_count=req.frames,
                            context=req.context or None)
    # 轮30:手工/agent 链路的终审凭证出口。该链路(平台 stitch 端点的
    # next_action 文档化:burn/mux/normalize → 本端点 → finalize)此前
    # 没有任何地方落盘 final_review.json,而 finalize 终审闸(轮25)只认
    # assemble 写的这个文件 → 整条手工链路被 409 NOT_REVIEWED 死锁。
    # verdict=pass 才落盘(pass 才是发布凭证);fix/blocked 不落盘。
    if req.project_id and str(_res.get("verdict") or "") == "pass":
        try:
            _d = _project_dir(req.project_id) / "final_review.json"
            _d.parent.mkdir(parents=True, exist_ok=True)
            _d.write_text(_json.dumps(_res, ensure_ascii=False, indent=1),
                          encoding="utf-8")
            try:
                _stage_store().record_event(
                    req.project_id, "final_review_passed",
                    "终验通过(手工链路凭证)", detail=str(_d))
            except Exception:
                pass
        except OSError:
            pass  # 落盘失败不改变终验结论本身
    return _res


# ── AI Agent Self-Discovery + Intake ─────────────────────────────
# These endpoints let any LLM/agent discover the platform contract and
# turn a user's vague intent into a reviewable brief — zero training.

# Brief dimensions enforced by the review engine (see engine._review_brief).
BRIEF_DIMENSIONS = {
    "content_type": "内容类型（short_drama 爽文短剧 / talking_head 口播 / product 产品 / montage 混剪 / narrative 剧情）",
    "product_info": "产品信息（推广的产品/品牌/素材，可为空字符串）",
    "target_platform": "目标平台（douyin/kuaishou/xiaohongshu/youtube，可多平台逗号分隔）",
    "duration_sec": "目标时长（秒，建议 15–180）",
    "target_audience": "目标受众（如 下沉市场、18-30男性 等）",
    "tone": "情绪基调（如 爽、燃、温情、悬疑）",
    "creative_direction": "创意方向（一个画面/一个冲突/一个钩子的描述）",
    "reference_materials": "参考素材（参考样片链接或风格描述，可为空）",
    "special_requirements": "特殊要求（字幕/比例/禁忌，可为空）",
}
# Extra optional fields the review engine tracks but does not gate on.
BRIEF_OPTIONAL = {
    "style_anchor": "视觉锚点：对画面风格的黄金描述（如 'golden light, cinematic film'）",
    "hook": "前3秒钩子描述",
    "ending": "结尾CTA/悬念设计",
}


class IntakeQuestionsRequest(BaseModel):
    answered: dict = {}
    intent: str = ""


class IntakeDraftRequest(BaseModel):
    answers: dict
    intent: str = ""


@app.get("/api/agent-guide")
def agent_guide():
    """AI self-discovery manual: the complete contract for driving the
    platform from a user intent to a finished video.  Every agent can read
    this endpoint (or AGENT_GUIDE.md) and act without prior knowledge."""
    return {
        "service": "shipin-platform",
        "version": "0.5.0",
        "language": "zh-CN",
        "core_principle": "每个阶段产出 JSON，先过 review 关口再进入下一阶段；review 的 manual_modes 反回给 LLM 重写，机械问题由服务端自动修复。音频铁律：AI 素材原生音轨必须保留（-map 0:a 连成环境床）；人声必须走配音角色库 /api/cast/roles（同一角色同一音色、多角色分色），禁止机器人式单音色旁白一刀切。故事铁律（AGENT_GUIDE §10.7.1）：分镜必须是五幕因果弧线（钩子→痛点→转折→延展→收束落版），相邻幕带 cause/effect 成对字段、收束幕回扣开篇意象、同一镜头全片≤3次；子代理若无法仅凭 sequence 顺序复述故事，即为素材罗列，重排顺序而不是加字幕掩盖。状态机铁律（§11）：下游操作携带 project_id 时，上游未 PASS 返回 409 BLOCKED；review/iterate pass 时自动 record_artifact；finalize 需 brief→script→storyboard→video_gen→post_production 全部 PASS。",
        "controlled_entry": {
            "desc": "推荐入口：受控执行（白名单+固定顺序+审查门+台账）。Agent 只需调 pipeline 四调用（text→confirm→generate→assemble）；guard 为底层受控骨架，普通运行无需直接触碰。误调内部 API 会被拒绝并留痕。stall/stop 或 blocked 时如实转述，不自行绕过。",
            "pipeline": {
                "text": "POST /api/pipeline/text {project_id, brief} → 服务端生成 brief审核→剧本→分镜→提示词 + review 循环",
                "confirm": "POST /api/project/confirm {project_id, gate, decision} → 人工确认闸门（script/storyboard）",
                "generate": "POST /api/pipeline/generate {project_id} → 首帧→链式尾帧→锚定视频→逐镜QC→TTS→对齐",
                "assemble": "POST /api/pipeline/assemble {project_id} → 转场→调色→字幕→声音设计→终验→RELEASED",
                "report": "GET /api/pipeline/{project_id}/report → 只读快照（阶段/闸门/manifest/QC/成本）"
            },
            "endpoints": {
                "start": "POST /api/guard/start {project_id, brief?} → 开始一次受控运行",
                "call": "POST /api/guard/call {run_id, function, params} → 只能调当前步骤白名单内函数",
                "review": "POST /api/guard/review {run_id} → 审查门；通过自动进入下一步",
                "manual_pass": "POST /api/guard/manual-pass {run_id, reason, approver} → 仅开启开关且已失败审查后可用",
                "status": "GET /api/guard/{run_id}/status → 当前步骤/可调函数/审查结论",
                "events": "GET /api/guard/{run_id}/events → 台账事件流回放"
            },
            "rules": [
                "未注册函数 → ok:false + code=UNKNOWN_FUNCTION，不执行",
                "跳步/乱序 → ok:false + code=NOT_ALLOWED_IN_STEP，消息含当前应处步骤",
                "审查未通过时调用下一步函数会被阻断；连败达上限运行变 blocked",
                "blocked 后只有人工放行（若该步骤开启）能解锁"
            ]
        },
"flow": [
            {"step": 1, "action": "POST /api/intake/questions", "note": "子问题清单：用自然语言向用户收集创意意图"},
            {"step": 2, "action": "POST /api/intake/draft", "note": "把用户回答组装成 brief JSON（缺失维度自动给默认值+标记）"},
            {"step": 3, "action": "POST /api/pipeline/text {project_id, brief}", "note": "受控阶段一（替代手工 review/iterate+LLM 写稿）：服务端例行 LLM 生成 brief审核→剧本→分镜→提示词，全部内嵌 review 循环；不收敛返回 blocked 与原因，逐字转述，禁止自行修改 JSON"},
            {"step": 4, "action": "POST /api/project/confirm {project_id, gate, decision}", "note": "唯一人工闸门：script/storyboard 双确认，未确认任何下游都是 409 BLOCKED；上报给用户决策，代理不得代确认"},
            {"step": 5, "action": "POST /api/pipeline/generate {project_id}", "note": "受控阶段二：首帧图→首尾帧链式策略→锚定视频→逐镜 QC（重试≤2）→TTS→旁白对齐，全确定性"},
            {"step": 6, "action": "POST /api/pipeline/assemble {project_id}", "note": "受控阶段三：对齐→落版卡→转场（链式=硬切/跳变=dissolve）→调色→字幕→声音设计（BGM 闪避+切点音效）→mux→归一化→双层终验→RELEASED"},
            {"step": 7, "action": "GET /api/pipeline/{project_id}/report", "note": "只读快照：阶段状态/确认闸门/manifest（首尾帧策略/逐镜 QC）/对齐结果；向用户展示从这里取数，不猜"},
            {"step": 8, "action": "GET /api/guard/{run_id}/status | /events", "note": "受控运行台账回放（只读）：当前步骤、可调白名单、审查结论。blocked 时唯一出路=改 brief 重跑 text 或人工放行"},
            {"step": 9, "action": "POST /api/ingest/reference", "note": "参考素材预解析（可选）：上传参考视频画像，返回 9 维参考档案；重跑 text 时带上 reference_id 让 brief 预填"},
            {"step": 10, "action": "失败策略（硬约束）", "note": "blocked/stall/stop/409 一律如实转述给用户；修复只有两条路：改 brief 重新 POST /api/pipeline/text，或人工放行；任何绕过（直接调低层 API 硬造素材）都会被 guard 留痕并拒绝"},
            {"step": 11, "action": "交付闭环", "note": "只在 report.status == RELEASED 且 manifest（素材池/首尾帧/QC/cost）齐全时宣布交付；否则继续步骤 10"},
        ],
        "endpoints": [
            {"method": "POST", "path": "/api/intake/questions", "body": {"intent": "用户模糊意图", "answer": "已答字段"}, "returns": "questions 列表（id/文案/是否必须/默认值）"},
            {"method": "POST", "path": "/api/intake/draft", "body": {"answers": "字段→答案", "intent": "用户意图"}, "returns": "brief + missing + 一轮 brief review"},
            {"method": "POST", "path": "/api/review/iterate", "body": {"stage": "brief|script|storyboard|image_prompt|video_prompt", "data": "任意阶段JSON", "max_rounds": 3}, "returns": "rounds[] + decision + data(修复后) + manual_modes[]"},
            {"method": "POST", "path": "/api/check/slideshow-risk", "body": {"scenes": []}, "returns": "6分类打分"},
            {"method": "POST", "path": "/api/check/variation", "body": {"scenes": []}, "returns": "8项变化检查"},
            {"method": "POST", "path": "/api/subtitle/transcribe", "body": {"audio_path": "绝对路径"}, "returns": "srt/json 路径"},
            {"method": "POST", "path": "/api/subtitle/burn", "body": {"video_path": "...", "srt_path": "...", "output_path": "...", "font_size": 46, "margin_v": 96, "mode": "auto"}, "returns": "ok/output/strategy/cues（逐cue found/width_pct/y_range）"},
            {"method": "POST", "path": "/api/video/stitch", "body": {"clips": ["..."], "output_path": "...", "transition": "cut", "transition_duration": 0.8}, "returns": "ok/output/duration"},
            {"method": "POST", "path": "/api/video/concat", "body": {"clips": ["..."], "output_path": "..."}, "returns": "ok/output"},
            {"method": "POST", "path": "/api/video/frames", "body": {"path": "...", "out_dir": "...", "interval": 1.0, "max_frames": 0}, "returns": "frames[{t,path}] + count —— 素材异常检查的抽帧工具，帧交VLM审查"},
            {"method": "POST", "path": "/api/audio/probe", "body": {"path": "..."}, "returns": "has_audio/verdict/mean_volume_db/lufs/silence_segments —— 成片放行硬门，verdict 必须为 ok"},
            {"method": "POST", "path": "/api/audio/duck", "body": {"primary": "...", "secondary": "...", "output": "...", "duck_level": -12}, "returns": "ok"},
            {"method": "POST", "path": "/api/audio/mix", "body": {"tracks": [{"path": "...", "role": "narration|music|sfx", "volume": 1.0}], "output": "..."}, "returns": "ok/output"},
            {"method": "POST", "path": "/api/audio/normalize", "body": {"input_audio": "...", "output_audio": "...", "target_lufs": -14.0}, "returns": "ok/measured_lufs"},
            {"method": "POST", "path": "/api/video/probe", "body": {"path": "..."}, "returns": "codec/resolution/fps/duration/audio"},
            {"method": "POST", "path": "/api/video/black-detect", "body": {"path", "min_dur"}, "returns": "[{start,end}]"},
        ],
        "review_contract": {
            "stages": ["brief", "script", "storyboard", "image_prompt", "video_prompt"],
            "decisions": {
                "pass": "通过",
                "pass_with_warnings": "通过含提示",
                "revise": "需修复（服务端自动机械修复后进入下一轮）",
                "stall": "无改善（达到轮数上限）",
                "stop": "停止（达到轮数上限，仍有 manual_modes 待人工/LLM处理）",
            },
            "rule": "decision 为 pass/pass_with_warnings 才放行进入下一步；manual_modes 里每一项都要求 LLM 重新生成对应字段后再 iterate",
            "brief_required_fields": sorted(BRIEF_DIMENSIONS),
        },
        "paths": "所有输入 media path 为服务器绝对路径；平台不生成素材，只做审查+剪辑+字幕+音频+质检",
        "tvc_quality": "AGENT_GUIDE §10.7：四拍结构（钩子/痛点/价值/落版）、剪辑节奏、品牌落版（slogan 大字+logo+收尾音）、短句文案入画（≤12字/句、大字焦点）、音效纵深（SFX≥3点对齐）、BGM ≥3 段情绪曲线、光感统一（同 color-grade）；成片缺任何一项都回炉。硬门补充：① /api/review/timeline 复用门 ② /api/review/final-video VLM 视觉门，任一 fail/blocked 即禁交付",
        "story_arc": "AGENT_GUIDE §10.7.1：分镜 sequence 必须构成五幕因果弧线——钩子(0-4s)→痛点(至1/3)→转折(1/3-2/3)→延展验证(2/3-4/5)→收束落版(结尾4-6s)，相邻幕写 cause/effect 成对字段；收束幕回扣开场意象（首尾闭环，只回扣一次）；同一镜头（同机位同景别）全片≤3次，第2次复用仅限回环且与首帧间隔≥20s；台词只在「该发声时刻」出现，旁白≤14字/句；交付前 VLM 逐幕抽帧验收，无法单靠 sequence 复述故事=素材罗列，重排素材顺序而非用字幕掩盖。硬门：① /api/review/timeline（素材复用≤3、间隔≥20s、单调递增、覆盖偏差≤3%）② /api/review/final-video（每批≤4帧×12帧，无断点/字幕可读/落版可见；AGNES_KEY 缺失则 blocked），任何一项非 pass → 禁止交付。状态机（§11）：任何下游操作携带 project_id 时上游未 PASS 则 409 BLOCKED；review/iterate pass 时自动 record_artifact；finalize 需 brief→script→storyboard→video_gen→post_production 全部 PASS。",
        "stage_machine": {
            "stages_order": ["brief", "script", "storyboard", "image_prompt",
                             "image_gen", "video_prompt", "video_gen",
                             "post_production"],
            "required_for_finalize": ["brief", "script", "storyboard",
                                      "video_gen", "post_production"],
            "project_create": "/api/project/create",
            "project_status": "/api/project/{id}/status",
            "project_finalize": "/api/project/{id}/finalize",
        },
    }


# ── 配音角色数据库（Voice Cast DB）────────────────────────────────
# 多音色配音的权威来源：同一角色永远映射到同一个 edge-tts 音色，
# 子智能体配音前必须先查本库（/api/cast/roles），禁止随手换声。

CAST_DB_PATH = _roots.data_dir() / "voice_cast.db"


def _cast_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(CAST_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cast_roles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role_code TEXT UNIQUE NOT NULL,
            role_name TEXT NOT NULL,
            gender TEXT NOT NULL,
            edge_voice TEXT NOT NULL,
            rate TEXT NOT NULL DEFAULT '-8%',
            style TEXT NOT NULL DEFAULT '平稳',
            note TEXT DEFAULT ''
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cast_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role_code TEXT NOT NULL,
            q_idx INTEGER NOT NULL,
            start_s REAL NOT NULL,
            end_s REAL NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            wav_path TEXT DEFAULT ''
        )"""
    )
    if conn.execute("SELECT COUNT(*) FROM cast_roles").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO cast_roles (role_code, role_name, gender, edge_voice, rate, style, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            CAST_SEED,
        )
    conn.commit()
    return conn


CAST_SEED = [
    # role_code, role_name, gender, edge_voice, rate, style, note
    ("hero_male", "男一号·青年创业者", "male", "zh-CN-YunxiNeural", "-8%",
     "沉稳自信，第一人称讲述产品感受", "主情绪线，承担产品价值句"),
    ("biz_female", "女声旁白", "female", "zh-CN-XiaoxiaoNeural", "-6%",
     "利落中性女声，开场与总结句", "用于钩子/收束，不与 hero 抢情绪"),
    ("colleague_male", "同事·背包人", "male", "zh-CN-YunjianNeural", "-6%",
     "稍轻快的男声，慌乱/感慨句", "只在开篇慌乱段与收束对比出现"),
    ("assistant_female", "助理·桌边人", "female", "zh-CN-XiaoyiNeural", "-6%",
     "年轻女声，一句反应台词", "可选短句，不占用主时间轴"),
]


class CastAssignRequest(BaseModel):
    role_code: str
    q_idx: int
    start_s: float
    end_s: float
    text: str


@app.get("/api/cast/roles")
def cast_roles():
    """回显角色库：谁用哪个音色、什么风格。子智能体配音前必查。"""
    conn = _cast_db()
    rows = conn.execute(
        "SELECT role_code, role_name, gender, edge_voice, rate, style, note "
        "FROM cast_roles ORDER BY id"
    ).fetchall()
    conn.close()
    return {"roles": [dict(r) for r in rows]}


@app.post("/api/cast/assign")
def cast_assign(req: CastAssignRequest):
    """把一条台词锁定给某个角色，写进 cast_lines（参数化写入）。"""
    conn = _cast_db()
    conn.execute(
        "INSERT INTO cast_lines (role_code, q_idx, start_s, end_s, text) "
        "VALUES (?, ?, ?, ?, ?)",
        (req.role_code, req.q_idx, req.start_s, req.end_s, req.text),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "assigned": req.role_code, "q_idx": req.q_idx}


@app.get("/api/cast/script")
def cast_script():
    """整片配音台本：按时间轴排好 谁×何时×说什么，供 TTS 生成。"""
    conn = _cast_db()
    rows = conn.execute(
        "SELECT c.q_idx, c.role_code, r.edge_voice, r.rate, c.text, c.start_s, c.end_s "
        "FROM cast_lines c JOIN cast_roles r ON c.role_code = r.role_code "
        "ORDER BY c.q_idx"
    ).fetchall()
    conn.close()
    return {"lines": [dict(r) for r in rows]}


# ── TTS / 配音 ────────────────────────────────────────────────────

class TtsNarrateRequest(BaseModel):
    script: dict
    output_dir: str = "./outputs"
    # 平台级：旁白写产物必须挂项目（script PASS 闸门、台账留痕）
    project_id: str


@app.post("/api/tts/narrate")
def tts_narrate(req: TtsNarrateRequest, request: Request):
    """为每个镜头合成旁白音频（同一角色映射同一音色）。平台级：必须挂项目，
    script 未 PASS 即 409，产物记入 video_gen + 成本台账。"""
    _enforce_project_binding(request, req.project_id)
    store = _stage_store()
    try:
        store.assert_stage_pass(req.project_id, "script")
    except StageGateError as e:
        raise _gate_error_response(e)

    tts = create_tts_service(work_dir=Path(req.output_dir), db_path=CAST_DB_PATH)
    segments = []
    for shot in req.script.get("shots", []):
        seg = tts.build_segment(
            shot_id=shot.get("shot_id", "S1"),
            text=shot.get("narration", ""),
            role_code=shot.get("role_code"),
            voice=shot.get("voice"),
            rate=shot.get("rate", "-6%"),
        )
        result = tts.synthesize_segments_sync([seg])[0]
        segments.append({
            "shot_id": result.shot_id,
            "text": result.text,
            "output_path": result.output_path,
            "duration_sec": result.duration_sec,
            "voice": result.voice,
            "error": result.error,
        })
    from shipin_platform.contracts import stable_artifact_hash
    store.record_artifact(req.project_id, "video_gen",
                          stable_artifact_hash({"tts_segments": segments}))
    record_cost(req.project_id, "tts", model="tts-v1",
                units=float(len(segments)), note="旁白")
    return {"ok": True, "segments": segments,
            "total_duration_sec": sum(s["duration_sec"] for s in segments)}


# ── 素材生成 ──────────────────────────────────────────────────────

class GenerateImageRequest(BaseModel):
    prompt: str
    width: int = 1920
    height: int = 1080
    output_path: str
    mode: str = "auto"
    # 平台级：生图是花钱+写产物步骤，必须挂项目（确认/状态机/成本）
    project_id: str
    shot_id: Optional[str] = None  # 该图对应哪个分镜（首帧锚定用）


@app.post("/api/generate/image")
def generate_image(req: GenerateImageRequest, request: Request):
    """图片生成：优先调用外部 API（Agnes/FLUX/OpenAI），无 key 时回退 PIL 占位图。
    必须挂 project（storyboard PASS 且 script 已用户确认才允许、成本照记）。"""
    _enforce_project_binding(request, req.project_id)
    store = _stage_store()
    try:
        store.assert_stage_pass(req.project_id, "storyboard")
        store.assert_confirmed(req.project_id, "script")
    except StageGateError as e:
        raise _gate_error_response(e)

    def _bill(model: str, note: str) -> None:
        record_cost(req.project_id, "image", model=model, units=1.0, note=note)

    if req.mode == "agnes":
        res = generate_image_agnes(req.prompt, req.width, req.height,
                                    req.output_path)
        if res.get("ok"):
            _bill("agnes-image", req.shot_id or "")
        return res
    flux_key = os.environ.get("FLUX_API_KEY", "").strip()
    openai_img_key = os.environ.get("OPENAI_IMAGE_API_KEY", "").strip()
    if req.mode == "flux" and flux_key:
        res = generate_image_flux(req.prompt, req.width, req.height,
                                   req.output_path, flux_key)
        if res.get("ok"):
            _bill("flux-pro", req.shot_id or "")
        return res
    if req.mode == "openai" and openai_img_key:
        res = generate_image_openai(req.prompt, req.width, req.height,
                                     req.output_path, openai_img_key)
        if res.get("ok"):
            _bill("dall-e-3", req.shot_id or "")
        return res
    # auto: 有 AGNES_KEY 则自动用 Agnes，否则 PIL 占位
    agnes_key = os.environ.get("AGNES_KEY", "").strip()
    if req.mode == "auto" and agnes_key:
        res = generate_image_agnes(req.prompt, req.width, req.height,
                                    req.output_path)
        if res.get("ok"):
            _bill("agnes-image", req.shot_id or "")
        return res
    return generate_image_pil(req.prompt, req.width, req.height,
                              req.output_path)


class GenerateVideoRequest(BaseModel):
    shots: list[dict]
    output_path: str
    fps: int = 24


@app.post("/api/generate/video")
def generate_video_endpoint(req: GenerateVideoRequest):
    """将图片序列合成视频（OpenMontage VideoStitch）。"""
    try:
        return generate_video(req.shots, req.output_path, req.fps)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Agnes 真实视频生成 ───────────────────────────────────────────

class GenerateAgnesVideoRequest(BaseModel):
    prompt: str
    model: str = "agnes-video-2.5-flash"  # 默认新一代：reference 双图锚定实测过 QC；
                                          # 旧协议可显式回退 agnes-video-2.5 / v2.0
    mode: str = "reference"              # reference(2.5) / ti2vid、keyframes(v2.0)；服务端按首尾帧自动选择
    duration: int = 5                    # 5 or 10 seconds（2.5 协议固定 5s，服务端拒收该字段时由平台裁剪）
    resolution: str = "720p"             # 720p or 1080p（2.5 协议固定 720p，服务端拒收该字段）
    output_path: str
    negative_prompt: str = ""
    # ── 首尾帧锚定（治"单镜头内私开子镜头"的核心约束）───────────────
    first_frame: Optional[str] = None    # 本镜首帧参考图（来自分镜生图）
    last_frame: Optional[str] = None     # 本镜尾帧参考图（通常=下一镜首帧或落版图）
    allow_unanchored: bool = False       # 显式豁免锚定（会记录警告，终验可见）
    # ── 状态机闸门 ──────────────────────────────────────────────────
    # 平台级：花钱步骤，必须挂项目
    project_id: str
    shot_id: Optional[str] = None


@app.post("/api/generate/agnes-video")
def generate_agnes_video(req: GenerateAgnesVideoRequest, request: Request):
    """Agnes AI 视频生成：有首尾帧走 keyframes 锚定，否则纯文生视频。
    必须挂 project；硬性闸门：storyboard PASS + script/storyboard 已确认
    + 首帧锚定（显式 allow_unanchored=true 可豁免）。成本照记。
    生成不等于可用——每个镜头还必须过 /api/qc/clip 才能进拼接。"""
    _enforce_project_binding(request, req.project_id)
    from pathlib import Path as _Path
    store = _stage_store()
    try:
        store.assert_confirmed(req.project_id, "script")
        store.assert_stage_pass(req.project_id, "storyboard")
        store.assert_confirmed(req.project_id, "storyboard")
    except StageGateError as e:
        raise _gate_error_response(e)
    if not req.first_frame and not req.allow_unanchored:
        raise HTTPException(status_code=422, detail={
            "code": "ANCHOR_REQUIRED",
            "message": ("未提供 first_frame——纯文生视频会在单镜头内私开子镜头"
                        "（『画面凌乱/换镜太快』病根）。请先为该分镜生成首帧图"
                        "（/api/generate/image），再以 keyframes 模式锚定生成；"
                        "确要跳过请显式传 allow_unanchored=true"),
            "shot_id": req.shot_id,
        })
    try:
        result = generate_video_agnes(
            prompt=req.prompt,
            model=req.model,
            duration=req.duration,
            resolution=req.resolution,
            work_dir=_Path(req.output_path).parent,
            first_frame=req.first_frame,
            last_frame=req.last_frame,
            output_path=req.output_path,
            negative_prompt=req.negative_prompt,
        )
        if result.get("ok"):
            record_cost(req.project_id, "video", model=req.model,
                        units=float(req.duration),
                        note=req.shot_id or "")
        result["shot_id"] = req.shot_id
        result["next_action"] = ("生成完成 ≠ 可用：立即调用 /api/qc/clip 校验该镜头"
                                 "（时长/镜内切/运动/首帧一致性），verdict=ok 才能拼接")
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/intake/questions")
def intake_questions(req: IntakeQuestionsRequest):
    """Return the questions to ask the user (or an LLM proxy) to obtain a
    review-complete brief.  `item` caches previously supplied answers."""
    known = set(req.answered or {})
    questions = []
    for key, prompt in BRIEF_DIMENSIONS.items():
        questions.append({
            "dimension": key,
            "question": prompt,
            "required": True,
            "answered": key in known,
            "default": _brief_default(key),
        })
    for key, prompt in BRIEF_OPTIONAL.items():
        questions.append({
            "dimension": key,
            "question": prompt,
            "required": False,
            "answered": key in known,
            "default": _brief_default(key),
        })
    return {"questions": questions, "note": "把必答题逐一向用户追问后，把答案提交到 /api/intake/draft"}


def _brief_default(key: str):
    defaults = {
        "content_type": "short_drama",
        "product_info": "",
        "target_platform": "抖音",
        "duration_sec": 60,
        "target_audience": "大众",
        "tone": "爽",
        "creative_direction": "",
        "reference_materials": "",
        "special_requirements": "",
        "style_anchor": "cinematic, natural light",
        "hook": "",
        "agent": "",
    }
    return defaults.get(key, "")


@app.post("/api/intake/draft")
def intake_draft(req: IntakeDraftRequest):
    """Assemble a review-complete draft brief from user answers.

    Every required dimension gets a value (user answer if provided, else a
    sensible default flagged in `defaulted`), then the brief passes one
    review round immediately so the caller can see model what is still weak.
    The response's `next` tells the agent the exact next call."""
    brief: dict = {}
    user_provided = set(req.answers)
    for key in BRIEF_DIMENSIONS:
        brief[key] = req.answers.get(key, _default_for(key))
    for key in BRIEF_OPTIONAL:
        if key in req.answers and req.answers[key]:
            brief[key] = req.answers[key]

    # Free-text intent: if creative_direction wasn't answered by the user,
    # the intent itself becomes the creative direction (default placeholder
    # is only there so the brief always has the dimension).
    if "creative_direction" not in user_provided and req.intent.strip():
        brief["creative_direction"] = req.intent.strip()

    missing = [k for k in BRIEF_DIMENSIONS if brief.get(k) in (None, "", 0)]
    engine = ReviewEngine()
    report = engine.run_review("brief", brief, round_num=1)

    return {
        "ok": True,
        "brief": brief,
        "missing": missing,
        "review": report.to_dict(),
        "next": {
            "action": "POST /api/review/iterate",
            "body": {"stage": "brief", "data": brief, "max_rounds": 3},
        },
    }


def _default_for(key: str) -> str:
    """Return a review-safe default for a missing brief dimension."""
    defaults = {
        "content_type": "short_drama",
        "product_info": "",
        "target_platform": "抖音",
        "duration_sec": 60,
        "target_audience": "大众",
        "tone": "爽",
        "creative_direction": "一个底层主角逆袭翻身的短视频节奏",
        "reference_materials": "",
        "special_requirements": "",
    }
    return defaults.get(key, "")


# ── Quality Check Endpoints ──────────────────────────────────────

@app.post("/api/check/slideshow-risk")
def check_slideshow_risk_api(req: ScenesRequest):
    return score_slideshow_risk(req.scenes)


@app.post("/api/check/variation")
def check_variation_api(req: ScenesRequest):
    return check_scene_variation(req.scenes)


# ── Subtitle Endpoints ───────────────────────────────────────────

@app.post("/api/subtitle/transcribe")
def transcribe_audio(req: SubtitleRequest):
    if WhisperService is None:
        raise HTTPException(status_code=503, detail="openai-whisper 未安装（可选依赖）；pip install openai-whisper 后可用")
    ws = WhisperService(model=req.model, device="cpu", language=req.language)
    try:
        result = ws.transcribe(Path(req.audio_path), output_dir=Path(req.output_dir))
        return {"ok": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/subtitle/burn")
def burn_subtitle(req: BurnSubtitleRequest):
    """Burn SRT into a video. Strategy auto-selects the implementation that
    actually paints glyphs on this host (libass probe → drawtext fallback) and
    returns per-cue objective measurements (`cues`) for the reviewer."""
    store = _stage_store()
    if req.project_id:
        try:
            # burn is post-production: require video_gen (stitch) + storyboard gate
            store.assert_stage_pass(req.project_id, "video_gen")
        except StageGateError as e:
            raise _gate_error_response(e)
    try:
        result = render_subtitles_best(
            req.video_path, req.srt_path, req.output_path,
            font_size=req.font_size, margin_v=req.margin_v,
            font_path=req.font_path or None, mode=req.mode,
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    # ── AGENT_GUIDE §10.6 字幕验收硬门 ────────────────────────────────
    # 轮27:内联实现提取为 subtitle_renderer.check_subtitle_cues 共享函数
    # (assemble 主链路同用);found=false 与宽度超红线 = critical 硬拦，
    # y 带问题为 warning(验收规则与 margin_v 默认排版的矛盾,不拦)。
    from shipin_platform.tools.subtitle_renderer import check_subtitle_cues
    violations = check_subtitle_cues(result.get("cues") or [],
                                     str(req.video_path))
    if any(v.get("severity") == "critical" for v in violations):
        result = {**result, "ok": False, "violations": violations}

    return {"ok": result["ok"], "output": result["output"],
            "strategy": result["strategy"], "font": result["font"],
            "libass_probe": result["libass_probe"],
            "cues": result["cues"],
            "violations": violations if violations else None}

# ── Final Release ────────────────────────────────────────────────

def _finalize_artifact_fails(project_id: str, fr: dict) -> list[dict]:
    """轮33/34:发布物三方哈希比对,返回 fails 列表(空=通过)。

    链条:盘上 final.mp4 sha256(`_file_sha256` 分块读,不全文件进内存)
    ↔ final_review.video_sha256(终审审的那条) ↔
    post_production.artifact_hash(assemble 混音归一后记的那条)。
    轮34:终审凭证缺 video_sha256 → CREDENTIAL_STALE(fail-closed)——
    /api/video/mux、/api/audio/normalize 带 project_id 会把 post_
    production 指纹顺手改写成新文件哈希,「旧凭证 + 可覆写指纹」让
    "换片再发布"对存量项目依然开放(四审审计 #1 实证)。恢复路径:
    对当前 final.mp4 重跑 /api/review/final-video 或重跑 assemble。
    """
    fails: list[dict] = []
    _final_mp4 = _project_dir(project_id) / "final.mp4"
    if not _final_mp4.is_file():
        return [{"gate": "final_artifact", "status": "MISSING",
                 "detail": "项目目录无 final.mp4"}]
    _disk_sha = _file_sha256(_final_mp4)
    _fr_sha = str((fr or {}).get("video_sha256") or "").strip()
    if not _fr_sha:
        fails.append({"gate": "final_artifact", "status": "CREDENTIAL_STALE",
                      "detail": ("终审凭证缺被审视频哈希(旧数据或读取失败)——"
                                 "对当前 final.mp4 重跑 "
                                 "/api/review/final-video 或重跑 assemble "
                                 "刷新凭证后再发布")})
    elif _fr_sha != _disk_sha:
        fails.append({"gate": "final_artifact",
                      "status": "REVIEW_VIDEO_MISMATCH",
                      "detail": ("盘上 final.mp4 与终审凭证记录的被审视频不一致"
                                 "——成片在过审后被换过;对当前 final.mp4 "
                                 "重跑 /api/review/final-video 刷新凭证")})
    try:
        _pp_row = _stage_store().get_stage(project_id,
                                           "post_production") or {}
        _pp_sha = str(_pp_row.get("artifact_hash") or "").strip()
    except Exception:
        _pp_sha = ""
    if _pp_sha and _pp_sha != "RELEASED" and _pp_sha != _disk_sha:
        fails.append({"gate": "final_artifact", "status": "ARTIFACT_MISMATCH",
                      "detail": ("盘上 final.mp4 与 post_production 记录的成片"
                                 "指纹不一致——发布物被改动,禁止发布")})
    return fails


@app.post("/api/project/{project_id}/finalize")
def project_finalize(project_id: str):
    """Mark project RELEASED: all required stages must be PASS and artifact
    hashes must match.  Returns list of unresolved gate errors if any stage
    is not yet passed — never lets a BLOCKED project through."""
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")

    # 轮32:幂等——已发布项目重复 finalize(前端重复点击/发布后轮询)旧行为
    # 走 required 循环,post_production 已是 RELEASED ≠ PASS → 409
    # UPSTREAM_FAILED,错误码语义是「上游未过」,实际是「早已发布」。
    # 轮33/34:幂等不等于免检——复用主路径同一套三方比对(含旧凭证
    # fail-closed),不一致落到完整门给 409,不再单写一套降格比较。
    _pp = store.get_stage(project_id, "post_production")
    if _pp is not None and str(_pp["status"]) == "RELEASED":
        _frp = _project_dir(project_id) / "final_review.json"
        _fr0: dict = {}
        try:
            if _frp.is_file():
                _fr0 = _json.loads(_frp.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            _fr0 = {}
        if not _finalize_artifact_fails(project_id, _fr0):
            return {"project_id": project_id, "status": "RELEASED",
                    "stages": store.get_project_status(project_id)}
        # 不一致:落到下面的完整门(终审闸+哈希比对)给出 409 明细

    # Required stages: brief through post_production (image_prompt/video_prompt
    # are optional when no generation step is used; skip them).
    required = ("brief", "script", "storyboard", "video_gen", "post_production")
    fails: list[dict] = []
    for stage in required:
        row = store.get_stage(project_id, stage)
        if row is None or row["status"] != "PASS":
            fails.append({"stage": stage, "status": row["status"] if row else "NOT_STARTED"})
    # 确认闸门：script 必须经用户确认过（防"一句话意图直接出片"）
    if store.get_confirmation(project_id, "script") is None:
        fails.append({"gate": "script_confirmed", "status": "NOT_CONFIRMED"})
    # 轮25 终审闸：finalize 此前只查阶段状态,从不读 final_review.json——
    # 终审 verdict=fix(VLM 断帧/品牌未入画/时间轴红线/旁白缺失/单镜与
    # 入拼 critical 全部并入)的成片照样能被置 RELEASED,所有门的阻断
    # 在发布入口被绕过。未跑过 assemble(无 final_review.json)同样拦。
    _fr_path = _project_dir(project_id) / "final_review.json"
    _fr: dict = {}
    try:
        if _fr_path.is_file():
            _fr = _json.loads(_fr_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _fr = {}
    _fr_verdict = str(_fr.get("verdict") or "")
    if not _fr_verdict:
        fails.append({"gate": "final_review", "status": "NOT_REVIEWED",
                      "detail": "未跑过 assemble 终审(final_review.json 缺失)"})
    elif _fr_verdict != "pass":
        _crit = [f for f in (_fr.get("findings") or [])
                 if f.get("severity") == "critical"]
        fails.append({"gate": "final_review", "status": _fr_verdict,
                      "critical": len(_crit),
                      "reason": str(_fr.get("reason") or "")[:200],
                      "detail": "终审未通过,按 findings 修复后重跑 assemble"})
    # 轮33:发布物三方哈希比对——终审闸此前只读 verdict 字段,「盘上的
    # final.mp4 就是当年过审的那条」从未被核对。assemble 跑通一次后
    # /api/video/mux 换个文件(3 个调用,无需任何故障注入)或直接换盘上
    # 文件再 finalize,轮25/27/30 的全部内容门在发布入口被整体绕过。
    # 轮34:verdict/阶段闸先报(最明确的 NOT_REVIEWED/fix),artifact
    # 闸作为第二层只对 verdict=pass 的项目生效;凭证缺 video_sha256
    # 按 fail-closed 拦(CREDENTIAL_STALE)。
    if fails:
        raise HTTPException(status_code=409,
                            detail={"code": "UPSTREAM_FAILED", "fails": fails})
    _art_fails = _finalize_artifact_fails(project_id, _fr)
    if _art_fails:
        raise HTTPException(status_code=409,
                            detail={"code": "ARTIFACT_MISMATCH",
                                    "fails": _art_fails})
    # 发布:保留下方记的成片完整性指纹,pipeline_runner 早已避开把 hash
    # 覆写成字面量"RELEASED"(那会让后续 finalize 的哈希校验永远 409),
    # 这里同样不能覆写;发布事件走 event,不动 artifact。指纹为空(旧数据/
    # mux 端点未记)时退回字面量,总比 record_artifact 的 EMPTY_HASH 500 好。
    row = store.get_stage(project_id, "post_production") or {}
    _pp_hash = str(row.get("artifact_hash") or "").strip()
    store.record_artifact(project_id, "post_production",
                          _pp_hash or "RELEASED", status="RELEASED")
    store.record_event(project_id, "released", "成片已发布(终验通过)",
                       stage="post_production")
    return {"project_id": project_id, "status": "RELEASED", "stages": store.get_project_status(project_id)}


# ── Video Stitching (OpenMontage VideoStitch) ────────────────────

@app.post("/api/video/stitch")
def stitch_videos(req: StitchRequest):
    """Stitch clips into a picture-native video. Gates when project_id given:
    storyboard PASS + script 确认 + **每个 clip 必须有 verdict=ok 的 /api/qc/clip
    记录**（按 clip_path 匹配）——未经 QC 通过的镜头永远进不了时间轴。"""
    store = _stage_store()
    if req.project_id:
        try:
            store.assert_stage_pass(req.project_id, "storyboard")
            store.assert_confirmed(req.project_id, "script")
        except StageGateError as e:
            raise _gate_error_response(e)
        unverified = []
        for c in req.clips:
            row = next((r for r in store.list_clip_qc(req.project_id)
                        if r["clip_path"] and
                        Path(r["clip_path"]).resolve() == Path(c).resolve()),
                       None)
            if row is None or row["verdict"] != "ok":
                unverified.append({
                    "clip": c,
                    "verdict": row["verdict"] if row else "NOT_QCED",
                })
        if unverified:
            raise HTTPException(status_code=409, detail={
                "code": "CLIP_QC_REQUIRED",
                "message": ("以下镜头未通过 /api/qc/clip 硬门（verdict != ok 或"
                            "从未 QC）——禁止拼接。先修复或重新生成，再逐镜 QC。"),
                "unverified": unverified,
            })
    engine = FFmpegEngine()
    if req.windows:
        # v2：旁白驱动窗口 + 保时长转场（总长 == sum(windows)，边界不动）
        result = build_transition_stitch(
            clips=req.clips, windows=req.windows, output=req.output,
            transition=req.transition if req.transition != "crossfade" else "dissolve",
            transition_duration=req.transition_duration, masters=req.masters,
            boundary_transitions=req.boundary_transitions)
        if not result.get("ok"):
            raise HTTPException(status_code=500, detail=result.get("error"))
    else:
        result = engine.stitch(
            [Path(c) for c in req.clips],
            Path(req.output),
            transition=req.transition,
            transition_duration=req.transition_duration,
        )
        if not result["ok"]:
            raise HTTPException(status_code=500, detail=result.get("error") or "stitch failed")
    # Record stitch artifact so downstream (burn/encode) can assert on it
    if req.project_id:
        from shipin_platform.contracts import stable_artifact_hash
        h = stable_artifact_hash({"clips": req.clips, "output": req.output})
        store.record_artifact(req.project_id, "video_gen", h)
        result["next_action"] = ("拼接完成：字幕/混音/归一化后必须调用 "
                                 "/api/review/final-video（带 context 分镜表）终验，"
                                 "verdict=pass 才能 finalize")
    return result


@app.post("/api/video/concat")
def concat_videos(req: StitchRequest):
    engine = FFmpegEngine()
    result = engine.concat([Path(c) for c in req.clips], Path(req.output))
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error") or "concat failed")
    return result


# ── Audio (OpenMontage AudioMixer) ───────────────────────────────

@app.post("/api/audio/duck")
def duck_audio_api(req: DuckRequest):
    engine = FFmpegEngine()
    result = engine.duck_audio(
        Path(req.primary_audio),
        Path(req.secondary_audio),
        Path(req.output_path),
        duck_level=req.duck_level,
    )
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error") or "duck failed")
    return result


@app.post("/api/audio/mix")
def mix_audio_api(req: MixRequest):
    engine = FFmpegEngine()
    result = engine.mix_audio(
        req.tracks,
        Path(req.output_path),
        normalize=req.normalize,
    )
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error") or "mix failed")
    return result


# ── Audio quality gate (deterministic, ffmpeg-only) ──────────────

class AudioProbeRequest(BaseModel):
    path: str


class FrameSampleRequest(BaseModel):
    path: str
    out_dir: str
    interval: float = 1.0
    max_frames: int = 0


@app.post("/api/audio/probe")
def audio_probe_api(req: AudioProbeRequest):
    """Full audio health report: stream facts, loudness, silence windows,
    verdict. Subagents MUST run this on the assembled master and only
    release when verdict == 'ok'."""
    from shipin_platform.tools.media_gates import audio_probe
    p = Path(req.path).resolve()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"not found: {p}")
    return audio_probe(p)


@app.post("/api/video/frames")
def sample_frames_api(req: FrameSampleRequest):
    """Sample frames from a video for vision-model asset sanity review.
    Returns frame paths; the caller feeds those to a VLM and refuses any
    segment whose frames show impossible geometry (two keyboards, two
    screens, extra hands, warped objects)."""
    from shipin_platform.tools.media_gates import extract_frames
    p = Path(req.path).resolve()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"not found: {p}")
    return extract_frames(p, req.out_dir, interval=req.interval,
                          max_frames=req.max_frames)


class NormalizeRequest(BaseModel):
    audio_path: str
    output_path: Optional[str] = None
    target_lufs: float = -14.0
    tp_dbtp: float = -1.5
    lra: float = 11.0
    two_pass: bool = False
    project_id: Optional[str] = None   # 传入即记录 post_production 阶段


@app.post("/api/audio/normalize")
def normalize_audio_api(req: NormalizeRequest):
    """Loudness-normalize to broadcast target (EBU R128 loudnorm)."""
    inp = Path(req.audio_path).resolve()
    if not inp.exists():
        raise HTTPException(status_code=404, detail=f"not found: {inp}")
    if req.output_path:
        out = Path(req.output_path).resolve()
    else:
        out = inp.with_name(f"{inp.stem}.normalized{inp.suffix}")

    af = f"loudnorm=I={req.target_lufs}:TP={req.tp_dbtp}:LRA={req.lra}"
    if req.two_pass:
        # Pass 1: measure — loudnorm prints its measurements as JSON on stderr.
        # print_format=json 是必需项：ffmpeg>=6 默认 none，不打印 JSON，
        # 此前 two_pass 因抓不到测量值而静默回退为单遍（结果偏差可达 3 LU）。
        r1 = subprocess.run(
            ["ffmpeg", "-y", "-i", str(inp), "-af", af + ":print_format=json",
             "-f", "null", "-"],
            capture_output=True, text=True, shell=False,
        )
        m = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', r1.stderr)
        if m:
            try:
                measured = _json.loads(m[-1])
                af = (
                    f"loudnorm=I={req.target_lufs}:TP={req.tp_dbtp}:LRA={req.lra}"
                    f":measured_I={measured.get('input_i')}"
                    f":measured_TP={measured.get('input_tp')}"
                    f":measured_LRA={measured.get('input_lra')}"
                    f":measured_thresh={measured.get('input_thresh')}"
                    f":offset={measured.get('target_offset')}:linear=true"
                )
            except _json.JSONDecodeError:
                pass  # fall back to one-pass with same filter

    # 编码器与输出扩展名保持一致：.wav 输出 PCM，否则 AAC。
    # 此前一律 AAC + ".wav" 后缀会让下游 probe 报 unreadable（容器/编码不匹配）。
    if str(out).lower().endswith(".wav"):
        enc = ["-c:a", "pcm_s16le", "-ar", "48000"]
    else:
        enc = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(inp), "-af", af, *enc, str(out)],
        capture_output=True, text=True, shell=False,
    )
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=r.stderr[-500:])
    if req.project_id:
        _record_post_production(req.project_id, out)
    return {"ok": True, "output": str(out), "two_pass": req.two_pass}


class MuxRequest(BaseModel):
    video_path: str
    audio_path: str
    output_path: str
    shortest: bool = True        # 音频比视频长时截断
    audio_offset: float = 0.0    # 旁白起点延迟（秒）
    project_id: Optional[str] = None   # 传入即记录 post_production 阶段


@app.post("/api/video/mux")
def mux_audio_video(req: MuxRequest):
    """把旁白音轨合入视频（此前平台没有任何端点能做这件事——音画合流缺口）。
    视频流直接 copy（无重编码、无质量损失），音频转 AAC 192k。
    这是 stitch（静音画面）与 audio/normalize（改响度不加轨）之间缺失的一环。"""
    v = Path(req.video_path).resolve()
    a = Path(req.audio_path).resolve()
    out = Path(req.output_path).resolve()
    if not v.exists():
        raise HTTPException(status_code=404, detail=f"video not found: {v}")
    if not a.exists():
        raise HTTPException(status_code=404, detail=f"audio not found: {a}")
    out.parent.mkdir(parents=True, exist_ok=True)
    offset_args = ["-itsoffset", str(req.audio_offset)] if req.audio_offset > 0 else []
    shortest_args = ["-shortest"] if req.shortest else []
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(v), "-i", str(a), *offset_args,
         "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         *shortest_args, str(out)],
        capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=r.stderr[-500:])
    if req.project_id:
        _record_post_production(req.project_id, out)
    return {"ok": True, "output": str(out),
            "video_stream": "copy", "audio_stream": "aac 192k",
            "next_action": "mux 后调 /api/audio/normalize 统一响度，再 /api/audio/probe + 终验"}


def _file_sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _record_post_production(project_id: str, output: Path) -> None:
    """记录 post_production 阶段（finalize 的前置条件之一）。
    历史教训：finalize 要求 post_production=PASS，但全平台没有任何端点写过
    它——记录代码写在 409 检查之后，永远走不到（coffee-v5 死锁实证）。"""
    store = _stage_store()
    try:
        store.create_project(project_id)
        store.record_artifact(project_id, "post_production",
                              _file_sha256(output))
    except StageGateError as e:
        raise _gate_error_response(e)


# ── Encode / Analysis ────────────────────────────────────────────

COLOR_PRESETS = {
    "warm_tvc": ("curves=r='0/0 0.3/0.28 0.7/0.72 1/1',"
                 "curves=g='0/0 0.5/0.5 1/1',"
                 "curves=b='0/0.05 0.7/0.68 1/0.95'"),
    "neutral": "curves=all='0/0 0.5/0.5 1/1'",
    "bright_social": "colorbalance=rsb=0.02:gsb=0.01",
}


@app.post("/api/video/color-grade")
def color_grade_video(req: ColorGradeRequest):
    inp = Path(req.video_path).resolve()
    out = Path(req.output_path).resolve()
    vf = COLOR_PRESETS.get(req.preset, COLOR_PRESETS["neutral"])
    r = subprocess.run([
        "ffmpeg", "-y", "-i", str(inp),
        "-vf", vf,
        "-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
        "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-movflags", "+faststart", str(out),
    ], capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=r.stderr[-500:])
    return {"ok": True, "output": str(out), "preset": req.preset}


@app.post("/api/video/encode")
def encode_video(req: EncodeRequest):
    inp = Path(req.input_path).resolve()
    out = Path(req.output_path).resolve()
    r = subprocess.run([
        "ffmpeg", "-y", "-i", str(inp),
        "-c:v", "libx264", "-crf", str(req.crf),
        "-preset", req.preset, "-profile:v", req.profile,
        "-level", "4.0", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart", str(out),
    ], capture_output=True, text=True, shell=False)
    if r.returncode != 0:
        raise HTTPException(status_code=500, detail=r.stderr[-500:])
    return {"ok": True, "output": str(out)}


@app.post("/api/video/probe")
def probe_video(req: MediaPathRequest):
    p = Path(req.path).resolve()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"not found: {p}")
    r = subprocess.run([
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,r_frame_rate",
        "-show_entries", "format=duration,size",
        "-of", "json", str(p),
    ], capture_output=True, text=True, shell=False)
    import json as _json
    data = _json.loads(r.stdout)
    s = data["streams"][0] if data.get("streams") else {}
    return {
        "codec": s.get("codec_name"),
        "width": s.get("width"),
        "height": s.get("height"),
        "fps": s.get("r_frame_rate"),
        "duration": float(data.get("format", {}).get("duration", 0)),
        "size_bytes": int(data.get("format", {}).get("size", 0)),
    }


@app.post("/api/video/black-detect")
def detect_black_frames(req: MediaPathRequest, min_dur: float = 0.5):
    from shipin_platform.analysis.reference_profiler import blackdetect_pix_arg
    p = Path(req.path).resolve()
    pix_arg = blackdetect_pix_arg()
    r = subprocess.run([
        "ffmpeg", "-i", str(p),
        "-vf", f"blackdetect=d={min_dur}:{pix_arg}=0.01",
        "-f", "null", "-",
    ], capture_output=True, text=True, shell=False)
    import re
    frames = []
    for line in (r.stdout + r.stderr).split("\n"):
        if "blackdetect" in line:
            m = re.search(r"black_start:([0-9.]+) black_end:([0-9.]+)", line)
            if m:
                frames.append({"start": float(m.group(1)), "end": float(m.group(2))})
    return {"black_frames": frames, "count": len(frames)}


class ReferenceAnalysisRequest(BaseModel):
    video_path: str
    scene_threshold: float = 0.3
    max_shots: int = 60


@app.post("/api/analysis/reference")
def analyze_reference(req: ReferenceAnalysisRequest):
    """B2 入口：本地参考视频确定性分析（元数据 + scene 镜头切分 + 节奏 +
    可并入 brief 的 hint）。只吃本地路径、不发任何网络请求。"""
    from shipin_platform.analysis.reference import (
        ReferenceError, analyze_reference_video)
    try:
        return analyze_reference_video(
            req.video_path,
            scene_threshold=req.scene_threshold,
            max_shots=req.max_shots)
    except ReferenceError as e:
        raise HTTPException(status_code=422, detail=str(e))


class IngestReferenceRequest(BaseModel):
    video_path: str
    name: Optional[str] = None
    scene_threshold: float = 0.3
    max_shots: int = 60


@app.post("/api/ingest/reference")
def ingest_reference(req: IngestReferenceRequest):
    """P1-② 入口 C：参考视频纵深剖析 → reference_report.json 落盘 +
    9 维 brief 预填。只吃本地路径、不发网络请求；报告存
    WIP 库外 data/reference_reports/。<name>.json。"""
    from shipin_platform.analysis.reference import ReferenceError
    from shipin_platform.analysis.reference_profiler import profile_reference
    import re as _re
    name = (req.name or "").strip()
    if name and not _re.fullmatch(r"[A-Za-z0-9_\-\.\u4e00-\u9fff]+", name):
        raise HTTPException(status_code=422,
                            detail=f"name 含非法字符: {name!r}")
    reports_dir = _roots.data_dir() / "reference_reports"
    try:
        return profile_reference(req.video_path, name=name or None,
                                 save_dir=reports_dir,
                                 scene_threshold=req.scene_threshold,
                                 max_shots=req.max_shots)
    except ReferenceError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── Variant (B3: 同源换参数重跑, 独立产物目录) ──────────────────

class VariantDeriveRequest(BaseModel):
    base_project_id: str
    variant_id: str
    overrides: dict = {}
    force: bool = False


class VariantRunRequest(BaseModel):
    phases: str = "all"
    category: Optional[str] = None


@app.post("/api/variant/derive")
def variant_derive(req: VariantDeriveRequest):
    """从基准成片派生变体:独立 dataRoot + 白名单参数覆盖,
    只读基准。校验失败返回 422。"""
    from shipin_platform.variants.variant_runner import (
        VariantError, derive_variant)
    try:
        return derive_variant(req.base_project_id, req.variant_id,
                              overrides=req.overrides, force=req.force)
    except VariantError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/variant/{variant_id}/run")
def variant_run(variant_id: str, req: VariantRunRequest):
    """变体重跑:phases=text|generate|assemble|all,阶段间硬门禁在阶段内。"""
    from shipin_platform.variants.variant_runner import (
        VariantError, run_variant_phases)
    # 轮42:变体重跑入口补预算硬闸——旧代码直接调 run_variant_phases,
    # 超预算项目经此车道继续烧 image/video/tts(七审 #1:sync/async 主
    # 车道有 _enforce_budget,variant/retry 两条车道完全失效)
    _enforce_budget(variant_id, _stage_store())
    try:
        return run_variant_phases(variant_id, _stage_store(),
                                  phases=req.phases, category=req.category)
    except VariantError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/variant/{variant_id}/status")
def variant_get_status(variant_id: str):
    """变体状态:档案(覆盖记录)+ 阶段 + 产物文件清单 + final 是否存在。"""
    from shipin_platform.variants.variant_runner import (
        VariantError, variant_status as _variant_status)
    try:
        return _variant_status(variant_id, _stage_store())
    except VariantError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ── Components (远期3: 组件化配方, 只读) ─────────────────────────

@app.get("/api/components")
def list_components():
    """组件配方清单(只读): 转场/落版卡/字幕/声音设计默认参数。
    不返回任何密钥;变体可从 variant_overridable 白名单覆盖。"""
    from shipin_platform.services.component_registry import (
        ComponentError, get_registry)
    try:
        reg = get_registry()
    except ComponentError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "ok": True,
        "components": [
            {"id": cid, "kind": spec.kind,
             "description": spec.description,
             "defaults": spec.defaults,
             "variant_overridable": list(spec.variant_overridable)}
            for cid, spec in
            ((cid, reg.get(cid)) for cid in reg.list_components())],
    }


# ── Pipeline ─────────────────────────────────────────────────────

# ── Pipeline（ComfyUI 式四调用编排：agent 零决策）─────────────────

class PipelineTextRequest(BaseModel):
    project_id: str
    brief: dict
    # 品类模板(对标 hypit SVS):tvc(默认)/drama/talk/tutorial
    category: Optional[str] = None
    # 参考素材画像(/api/ingest/reference 的落盘名)：预填 brief 空维度
    reference_id: Optional[str] = None
    # C5: USD 预算硬闸(学 screenshot-to-code cost gate / ViMax 成本失控教训)
    max_budget_usd: Optional[float] = None


def _reference_preloaded(reference_id: str) -> dict:
    """读取参考报告的 brief_prefill，只取 filled/suggested 维度的值。"""
    refs_dir = _roots.data_dir() / "reference_reports"
    fp = refs_dir / f"{reference_id}.json"
    if not fp.is_file():
        raise HTTPException(
            status_code=404,
            detail=f"参考档案不存在: {reference_id!r}。"
                   "先 POST /api/ingest/reference 生成档案。",
        )
    try:
        report = _json.loads(fp.read_text(encoding="utf-8"))
    except _json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="参考档案损坏（非 JSON）")
    prefill = report.get("brief_prefill") or {}
    return {k: v.get("value") for k, v in prefill.items()
            if isinstance(v, dict) and v.get("state") in ("filled", "suggested")}


class PipelineRunRequest(BaseModel):
    project_id: str


@app.post("/api/pipeline/text")
def pipeline_text(req: PipelineTextRequest, request: Request):
    _enforce_project_binding(request, req.project_id)
    """阶段一：服务端 LLM 生成 brief 审查→剧本→分镜→提示词（全部带审核循环，
    不收敛即 blocked）。agent 只需把返回的剧本/分镜表展示给用户等确认。
    带 reference_id 时先注入参考画像预填，再进审查循环。"""
    brief = dict(req.brief)
    if req.reference_id:
        # 参考预填只填空缺维度（用户已有值优先），再溯源
        for k, v in _reference_preloaded(req.reference_id).items():
            if not brief.get(k):
                brief[k] = v
        brief.setdefault("_reference_id", req.reference_id)
    store = _stage_store()
    try:
        store.create_project(req.project_id)
    except StageGateError as e:
        raise _gate_error_response(e)
    if req.max_budget_usd is not None:
        _write_budget(req.project_id, req.max_budget_usd, store)
    r = run_text_phase(req.project_id, brief, store, category=req.category)
    return r


@app.post("/api/pipeline/generate")
def pipeline_generate(req: PipelineRunRequest, request: Request):
    _enforce_project_binding(request, req.project_id)
    if request.query_params.get("async") == "true":
        return _enqueue_phase(request, "generate", req.project_id)
    """阶段二：首帧图→首帧尾帧链式策略（chain=下一镜首帧 / own_end=自动生成
    自末帧）→锚定视频→逐镜 QC（重试≤2）→TTS→旁白对齐。闸门：script/
    storyboard 双确认。入口先查预算硬闸（C5：超限拒绝，账目随 report 暴露）。"""
    store = _stage_store()
    _enforce_budget(req.project_id, store)
    store.record_event(req.project_id, "phase_started",
                       "阶段二 generate 启动（首帧/链式视频/TTS）", stage="video_gen")
    r = run_generate_phase(req.project_id, store)
    store.record_event(req.project_id, "phase_finished",
                       f"阶段二 generate 结束：{'ok' if r.get('ok') else 'failed'}",
                       stage="video_gen",
                       detail=str(r.get("reason") or
                                  f"{len(r.get('report') or [])} 镜"))
    return r


@app.post("/api/pipeline/assemble")
def pipeline_assemble(req: PipelineRunRequest, request: Request):
    _enforce_project_binding(request, req.project_id)
    if request.query_params.get("async") == "true":
        return _enqueue_phase(request, "assemble", req.project_id)
    """阶段三：对齐→落版卡→逐边界转场（链式=硬切/跳变=dissolve）→调色→
    字幕→声音设计（BGM 闪避+切点音效）→mux→归一化→终验→RELEASED。"""
    store = _stage_store()
    _enforce_budget(req.project_id, store)
    store.record_event(req.project_id, "phase_started",
                       "阶段三 assemble 启动", stage="post_production")
    r = run_assemble_phase(req.project_id, store)
    store.record_event(req.project_id, "phase_finished",
                       f"阶段三 assemble 结束：{'released' if r.get('released') else 'failed'}",
                       stage="post_production",
                       detail=str(r.get("reason") or ""))
    r["preview_frames"] = _refresh_preview_frames(req.project_id)
    return r


# ── P1 异步任务（202 + 轮询；进度由 stage_runs 推导，零侵入）──────────

@app.get("/api/tasks/{task_id}")
def task_status(task_id: str, request: Request):
    """异步任务详情：status/attempts/progress/current_stage/result。"""
    ts = _task_store()
    ts.recover_stale()
    task = ts.task_status(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _enforce_task_binding(request, task)
    return task


@app.get("/api/tasks")
def list_tasks(request: Request, project_id: Optional[str] = None,
               limit: int = 200):
    """异步任务列表（最新在前）。绑定项目 key 只能看自己项目的任务。"""
    ts = _task_store()
    ts.recover_stale()
    principal = getattr(getattr(request, "state", None), "principal", None)
    if principal is not None and not principal.is_admin and principal.project_id:
        project_id = principal.project_id
    rows = ts.list_tasks(project_id=project_id, limit=limit)
    return {"tasks": [ts.task_status(r["task_id"]) for r in rows]}


@app.post("/api/tasks/{task_id}/retry")
def task_retry(task_id: str, request: Request):
    """幂等重试（借鉴 Airflow UP_FOR_RETRY）：仅 failed/retry 可重投。"""
    ts = _task_store()
    ts.recover_stale()
    task = ts.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    _enforce_task_binding(request, task)
    # 轮42:重投入口补预算硬闸(七审 #1:task_store.retry_task 重放的 fn
    # 直接跑 run_generate_phase,闸门没挂在 fn 上,重投即绕过;只对
    # 花钱的 generate/assemble 类任务生效)
    if str(task.get("kind") or "") in ("generate", "assemble", "all"):
        _enforce_budget(str(task.get("project_id") or ""), _stage_store())
    retried = ts.retry_task(task_id)
    if retried is None:
        raise HTTPException(
            status_code=409,
            detail=f"task not retryable (status='{task['status']}')")
    return retried


@app.get("/api/pipeline/{project_id}/report")
def pipeline_report(project_id: str):
    """项目全量快照：阶段状态/确认闸门/manifest（首尾帧策略/逐镜 QC）/对齐/成本/预算。"""
    store = _stage_store()
    from shipin_platform.orchestration.pipeline_runner import _load
    brief = _load(project_id, "brief.json") or {}
    costs = cost_summary(project_id)
    budget = _read_budget(project_id)
    return {"project_id": project_id,
            "stages": store.get_project_status(project_id),
            "confirmations": {g: store.get_confirmation(project_id, g)
                              for g in ("brief", "script", "storyboard")},
            "clip_qc": store.list_clip_qc(project_id),
            "manifest": _load(project_id, "manifest.json"),
            "reference_id": brief.get("_reference_id"),
            "costs": costs,
            "budget": budget,
            "preview_frames": _preview_frame_urls(project_id)}


# ── C5 阶段协同：中间产物可见 / 人工介入 / 预算闸 / 执行轨迹 ──────────
# 对标（见 docs/2026-09-17-C5-阶段协同与人工介入-调研与设计.md）：
#   n8n/Dify 执行轨迹 → GET /events；Airflow 任务产物 → GET /artifact；
#   ComfyUI 改 prompt 重排 → POST /rewrite；windmill 审批前置 →
#   /budget 硬闸 + /preflight 体检。全部同源 REST，无外部 URL 请求。

_BUDGET_FILE = "budget.json"
_REWRITABLE_STAGES = ("script", "storyboard")
# 每阶段需要的最小形状（宽松校验：只拦"明显坏了"，不拦微调）
_REWRITE_MIN_SHAPE = {"script": ("shots",), "storyboard": ("shots",)}
_ARTIFACT_FILES = {
    "brief": "brief.json",
    "script": "script.json",
    "storyboard": "storyboard.json",
    "image_prompt": "image_prompt.json",
    "video_prompt": "video_prompt.json",
    "manifest": "manifest.json",
    "stitch": "stitch_result.json",
    "final_review": "final_review.json",
}


def _project_dir(project_id: str) -> Path:
    return _shipin_root / "data" / "projects" / project_id


def _read_budget(project_id: str) -> Optional[dict]:
    """读项目预算配置（未设置 → None）。纯本地文件，无网络/无状态改写。"""
    fp = _project_dir(project_id) / _BUDGET_FILE
    if not fp.is_file():
        return None
    try:
        data = _json.loads(fp.read_text(encoding="utf-8"))
    except (_json.JSONDecodeError, OSError):
        return None
    return {"max_budget_usd": data.get("max_budget_usd"),
            "set_at": data.get("set_at")}


def _write_budget(project_id: str, max_usd: Optional[float], store) -> dict:
    """设置/解除项目预算上限（None=解除）。写入 data/ 下的项目目录，
    不进封印清单（运行时数据）。"""
    if max_usd is not None and max_usd < 0:
        raise HTTPException(status_code=422, detail="max_budget_usd 不能为负")
    d = _project_dir(project_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / _BUDGET_FILE).write_text(
        _json.dumps({"max_budget_usd": max_usd, "set_at": _now_iso()},
                    ensure_ascii=False, indent=1), encoding="utf-8")
    store.record_event(project_id, "budget_set",
                       f"预算上限 {'解除' if max_usd is None else f'设为 ${max_usd}'}",
                       stage="brief",
                       detail=f"max_budget_usd={max_usd}")
    return _read_budget(project_id)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _enforce_budget(project_id: str, store) -> None:
    """花钱阶段入口硬闸（学 screenshot-to-code $3 gate）：先查全局月度闸
    （P3 OpenCost 式 showback），再查项目闸；超限直接 422，不开始任何生成。
    只读预算 + 记一条事件，不改任何状态。"""
    exceeded, used, mx = global_budget_exceeded()
    if exceeded:
        store.record_event(
            project_id, "budget_exceeded",
            f"全局月度预算超限：本月已用 ${used:.4f} > 上限 ${mx:.4f}",
            stage="summary", detail=f"blocked={_now_iso()} (global)")
        raise HTTPException(
            status_code=422,
            detail=f"全局月度预算超限：已用 ${used:.4f} > 上限 ${mx:.4f}。"
                   f"请先由 admin POST /api/platform/budget 调整后再重试")
    b = _read_budget(project_id)
    if not b or b["max_budget_usd"] is None:
        return
    try:
        used = cost_summary(project_id)["total_usd"]
    except LedgerCorruptError as e:
        # 轮43(七审 #5):账本损坏绝不静默放行——旧 _load_rows 损坏时返回
        # [] 让预算闸判定"没花钱"重新放行,历史账目无声消失
        store.record_event(
            project_id, "ledger_corrupt",
            f"成本账本损坏,预算核对中止: {str(e)[:160]}",
            stage="summary", detail=f"blocked={_now_iso()} (ledger)")
        raise HTTPException(
            status_code=422,
            detail=f"成本账本损坏、无法核对预算: {str(e)[:160]}。"
                   f"请修复或删除 data/projects/{project_id}/cost.json "
                   f"的损坏备份后重试")
    if used > b["max_budget_usd"]:
        store.record_event(
            project_id, "budget_exceeded",
            f"预算超限：已用 ${used:.4f} > 上限 ${b['max_budget_usd']}",
            stage="summary", detail=f"blocked={_now_iso()}")
        raise HTTPException(
            status_code=422,
            detail=f"预算超限：已用 ${used:.4f} > 上限 $"
                   f"{b['max_budget_usd']}。请先 POST /api/pipeline/"
                   f"{project_id}/budget 调整上限再重试")


def _preview_frame_urls(project_id: str) -> list[str]:
    d = _project_dir(project_id) / "preview_frames"
    if not d.is_dir():
        return []
    names = sorted(p.name for p in d.glob("*.jpg"))
    return [f"/api/pipeline/{project_id}/preview/{n}" for n in names]


class RewriteStageRequest(BaseModel):
    stage: str
    content: dict


class BudgetRequest(BaseModel):
    max_budget_usd: Optional[float] = None


@app.get("/api/pipeline/{project_id}/events")
def pipeline_events(project_id: str, limit: int = 50):
    """执行轨迹（n8n execution / Dify trace 的轻量版）：阶段动作/人工介入/
    AI 经 MCP 调用自动留痕，前端 5s 轮询即成"AI 干了什么"时间线。"""
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    return {"project_id": project_id,
            "events": store.list_events(project_id, limit=limit)}


@app.get("/api/pipeline/{project_id}/artifact")
def pipeline_artifact(project_id: str, stage: str):
    """中间产物内容（Airflow 任务产物视角）：script/storyboard/image_prompt/
    video_prompt/manifest/final_review 等 JSON 原样返回，供前端展示与编辑。"""
    fname = _ARTIFACT_FILES.get(stage)
    if fname is None:
        raise HTTPException(
            status_code=422,
            detail=f"未知产物 stage={stage!r}，可选：" + ", ".join(_ARTIFACT_FILES))
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    fp = _project_dir(project_id) / fname
    if not fp.is_file():
        return {"stage": stage, "file": fname, "exists": False, "content": None}
    try:
        content = _json.loads(fp.read_text(encoding="utf-8"))
    except (_json.JSONDecodeError, OSError) as e:
        return {"stage": stage, "file": fname, "exists": True,
                "content": None, "parse_error": str(e)}
    return {"stage": stage, "file": fname, "exists": True, "content": content}


@app.post("/api/pipeline/{project_id}/rewrite")
def pipeline_rewrite(project_id: str, req: RewriteStageRequest):
    """人工介入改产物（ComfyUI 改 prompt 重排 的管控版）：仅允许 script/
    storyboard；写回文件→清确认→该阶段置 PENDING→下游全部失效→事件留痕。
    之后由用户重新确认并走 generate 重跑，杜绝"改完旧链条继续花钱"。"""
    if req.stage not in _REWRITABLE_STAGES:
        raise HTTPException(
            status_code=422,
            detail=f"仅 {_REWRITABLE_STAGES} 可人工改写；"
                   f"image_prompt/video_prompt 由分镜确定性派生，请改上游")
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    if not isinstance(req.content, dict):
        raise HTTPException(status_code=422, detail="content 必须是 JSON 对象")
    for field in _REWRITE_MIN_SHAPE[req.stage]:
        val = req.content.get(field)
        if field == "shots" and (not isinstance(val, list) or not val):
            raise HTTPException(status_code=422, detail=f"{field} 必须是非空数组")
    # 形状宽松校验后写回（用户版本权优先，规则闸门交给 review 阶段）
    from shipin_platform.orchestration.pipeline_runner import _save
    _save(project_id, f"{req.stage}.json", req.content)
    store.clear_confirmation(project_id, req.stage)
    store.reset_stage(project_id, req.stage)
    n = store.invalidate_downstream(project_id, req.stage)
    ev = store.record_event(
        project_id, "user_rewritten",
        f"用户改写 {req.stage}（{len(_json.dumps(req.content, ensure_ascii=False))} 字节）",
        stage=req.stage, detail=f"下游 {n} 个阶段已失效 → 重新确认后再跑")
    return {"ok": True, "stage": req.stage, "invalidated": n,
            "next": f"重新确认 gate={req.stage} 后 POST /api/pipeline/"
                    f"generate 重跑下游", "event": ev}


@app.get("/api/pipeline/{project_id}/versions/{stage}/{version}")
def pipeline_version_content(project_id: str, stage: str, version: int):
    """P5 版本内容读取（diff 视图数据源）：按版本号取全量快照内容。"""
    from shipin_platform.services.artifact_store import (
        TRACKED_STAGES, read_version)
    from shipin_platform.orchestration.pipeline_runner import _project_dir
    if stage not in TRACKED_STAGES:
        raise HTTPException(
            status_code=422,
            detail=f"仅 {list(TRACKED_STAGES)} 支持版本读取")
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404,
                            detail=f"project not found: {project_id}")
    entry, content = read_version(_project_dir(project_id), stage, version)
    if entry is None or content is None:
        raise HTTPException(
            status_code=404,
            detail=f"{stage} 无版本 v{version}")
    return {"stage": stage, "version": version, "hash": entry["hash"],
            "ts": entry["ts"], "caller": entry.get("caller"),
            "content": content}


@app.get("/api/pipeline/{project_id}/versions")
def pipeline_versions(project_id: str, stage: Optional[str] = None):
    """P2 版本索引（DVC 式元数据指针）：各阶段历史版本（v/hash/ts/caller）。"""
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404,
                            detail=f"project not found: {project_id}")
    from shipin_platform.services.artifact_store import all_versions
    from shipin_platform.orchestration.pipeline_runner import _project_dir
    index = all_versions(_project_dir(project_id))
    if stage:
        entries = index.get(stage, [])
        index = {stage: entries} if entries else {}
    return {"project_id": project_id, "versions": index}


class RestoreRequest(BaseModel):
    stage: str
    version: int


@app.post("/api/pipeline/{project_id}/restore")
def pipeline_restore(project_id: str, req: RestoreRequest):
    """P2 回滚（dvc checkout 语义）：写回旧版产物 → 清确认 → 闸门重置 →
    下游全部失效 → 事件留痕。与 rewrite 同款「改完旧链条不再花钱」栅栏。"""
    from shipin_platform.services.artifact_store import (
        TRACKED_STAGES, read_version)
    from shipin_platform.orchestration.pipeline_runner import _project_dir, _save
    if req.stage not in TRACKED_STAGES:
        raise HTTPException(
            status_code=422,
            detail=f"仅 {list(TRACKED_STAGES)} 支持版本回滚")
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404,
                            detail=f"project not found: {project_id}")
    entry, content = read_version(_project_dir(project_id), req.stage,
                                  req.version)
    if entry is None or content is None:
        raise HTTPException(
            status_code=404,
            detail=f"{req.stage} 无版本 v{req.version}")
    _save(project_id, f"{req.stage}.json", content)
    store.clear_confirmation(project_id, req.stage)
    store.reset_stage(project_id, req.stage)
    n = store.invalidate_downstream(project_id, req.stage)
    ev = store.record_event(
        project_id, "stage_restored",
        f"回滚 {req.stage} 到 v{req.version}（hash {entry['hash'][:12]}…）",
        stage=req.stage,
        detail=f"caller={entry.get('caller') or 'auto'}；"
               f"下游 {n} 个阶段已失效 → 重新确认后再跑")
    return {"ok": True, "stage": req.stage, "version": req.version,
            "hash": entry["hash"], "invalidated": n, "event": ev}


@app.post("/api/pipeline/{project_id}/budget")
def pipeline_budget(project_id: str, req: BudgetRequest):
    """设置/解除项目预算（windmill 审批前置的硬闸版）。generate/assemble
    入口依据此硬性拒绝，杜绝 vi-max 式成本失控。"""
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    budget = _write_budget(project_id, req.max_budget_usd, store)
    return {"project_id": project_id, "budget": budget,
            "current_usd": cost_summary(project_id)["total_usd"]}


@app.post("/api/pipeline/{project_id}/preflight")
def pipeline_preflight(project_id: str):
    """只读体检（Prefect/Argo preflight 意识）：凭据/前置产物/闸门/预算/assets，
    一个都不生成，只回答"现在能不能跑"。"""
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    checks = []

    def add(name, ok, detail):
        checks.append({"name": name, "ok": ok, "detail": detail})

    # 1) 关键凭据（只报有/无，永不回显 key）
    creds_ok = True
    prov = {}
    try:
        prov = _json.loads(
            (_roots.config_dir() / "providers.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    cred_cfg = (prov.get("credentials")) if isinstance(prov.get("credentials"), dict) else {}
    cred_required = [name for name, c in cred_cfg.items()
                     if isinstance(c, dict) and c.get("env")]
    for name in cred_required:
        env_name = cred_cfg[name].get("env", "")
        ok = bool(os.environ.get(env_name, "").strip())
        creds_ok = creds_ok and ok
        add(f"credential:{name}", ok, f"{env_name} "
            + ("已配置" if ok else "未配置（生成将被明确报错，不会偷偷换凭据）"))
    if not cred_required:
        add("credentials", True, "providers.json 无凭据声明")

    # 1b) key 池（seal 配置 + 登记数；只读，不暴露任何 key）
    pool_status = _keypool_status()
    add("key_pool", bool(pool_status.get("seal_configured"))
        and pool_status.get("count") is not None,
        pool_status.get("detail", ""))

    # 2) 前置产物
    from shipin_platform.orchestration.pipeline_runner import _load
    for art in ("brief.json", "script.json", "storyboard.json"):
        add(f"artifact:{art}", _load(project_id, art) is not None,
            "存在" if _load(project_id, art) is not None else "缺失")
    # 3) 状态机状态
    st = store.get_project_status(project_id)
    for st_name in ("script", "storyboard"):
        row = st.get(st_name)
        add(f"stage:{st_name}",
            row is not None and row["status"] == "PASS",
            (row or {}).get("status", "未开始"))
    # 4) 闸确认
    for g in ("brief", "script", "storyboard"):
        add(f"gate:{g}", store.get_confirmation(project_id, g) is not None,
            "已确认" if store.get_confirmation(project_id, g) else "未确认")
    # 5) 预算
    b = _read_budget(project_id)
    if b and b["max_budget_usd"] is not None:
        used = cost_summary(project_id)["total_usd"]
        add("budget", used <= b["max_budget_usd"],
            f"已用 ${used:.4f}/上限 ${b['max_budget_usd']}"
            + ("" if used <= b["max_budget_usd"] else "（超限）"))
    else:
        add("budget", True, "未设上限")
    # 6) 产物目录 & 宽表
    add("project_dir", _project_dir(project_id).is_dir(), "可写")
    # 7) P7 资源健康：限流状态 / 磁盘余量 / 审计日志规模（决策前置信息）
    import shutil
    try:
        du = shutil.disk_usage(_shipin_root)
        disk_free = du.free
        add("disk", disk_free > 512 * 1024 * 1024,
            f"free {disk_free // (1024 * 1024)} MB / "
            f"{du.total // (1024 * 1024 * 1024)} GB")
    except OSError:
        add("disk", True, "磁盘状态不可读（跳过）")
    rs = rate_state()
    add("rate_limit", rs["mode"] == "on",
        f"mode={rs['mode']} max={rs['max']}/{rs['window']}s "
        f"active_buckets={rs['active_buckets']}")
    add("audit_log", True, f"rows={audit_store.count()}")
    return {"project_id": project_id, "ok": all(c["ok"] for c in checks),
            "checks": checks, "platform": {
                "rate": {k: rs[k] for k in
                         ("mode", "max", "window", "ip_max")},
                "audit_rows": audit_store.count()}}


def _probe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=10)
        return float(r.stdout.strip().split(",")[0])
    except Exception:
        return 0.0


def _refresh_preview_frames(project_id: str, n_frames: int = 3) -> list[str]:
    """assemble 后抽帧预览（ComfyUI 输出预览的轻量版）：final.mp4 三帧
    → data/projects/<id>/preview_frames/。纯本地 FFmpeg，零网络。"""
    final = _project_dir(project_id) / "final.mp4"
    out = _project_dir(project_id) / "preview_frames"
    out.mkdir(parents=True, exist_ok=True)
    if not final.is_file():
        return []
    dur = _probe_duration(final)
    if dur <= 0:
        return []
    from shipin_platform.tools.media_gates import extract_frames
    try:
        res = extract_frames(str(final), str(out),
                             interval=max(dur / n_frames, 0.5),
                             start=0.0, max_frames=n_frames)
        got = [f["path"] for f in res.get("frames", [])]
    except Exception:
        got = []
    if not got:
        return []
    return [f"/api/pipeline/{project_id}/preview/{Path(p).name}" for p in got]


@app.get("/api/pipeline/{project_id}/preview")
def pipeline_preview(project_id: str):
    """成片预览帧列表（assemble 后自动生成 3 帧缩略图）。"""
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    return {"project_id": project_id, "frames": _preview_frame_urls(project_id)}


@app.get("/api/pipeline/{project_id}/preview/{name}")
def pipeline_preview_file(project_id: str, name: str):
    """预览帧图片（白名单：仅 .jpg 文件名，resolve 前缀校验防穿越）。"""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.jpg", name or ""):
        raise HTTPException(status_code=404, detail="not found")
    base = (_project_dir(project_id) / "preview_frames").resolve()
    p = (base / name).resolve()
    if not str(p).startswith(str(base) + os.sep) or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    from fastapi.responses import FileResponse
    return FileResponse(p, media_type="image/jpeg")


class PipelineRequest(BaseModel):
    brief: dict


@app.post("/api/pipeline/run")
def run_pipeline(req: PipelineRequest):
    """已废弃（410）：旧 review-only 流水线，无项目/台账/状态机。

    平台级约束：AI 只能走受控编排入口（/api/guard/start → /api/pipeline/text
    → /api/project/confirm → /api/pipeline/generate → /api/pipeline/assemble）。
    本端点从不执行任何工作，仅告知调用方迁移。"""
    raise HTTPException(
        status_code=410,
        detail="POST /api/pipeline/run 已废弃（review-only 老流水线，无项目上下文）。"
               "请使用受控入口：POST /api/pipeline/text {project_id, brief} → "
               "POST /api/project/confirm → POST /api/pipeline/generate → "
               "POST /api/pipeline/assemble",
    )


# ── 过程回放（timeline）：把推进轨迹聚合成可读时间线 ──────────────


@app.get("/api/pipeline/{project_id}/timeline")
def pipeline_timeline(project_id: str):
    """「过程回放」聚合端点：AI/人各干过什么，一屏看全。

    合并：阶段里程碑（状态机 stage PASS/FAILED/BLOCKED + 更新时间）、
    执行事件账本（pipeline_events 旧→新）、产物资产（图/视频）、
    逐镜 QC 门禁、版本历史计数与成本汇总。只读，无副作用。
    """
    store = _stage_store()
    try:
        store._require_project(project_id)
    except StageGateError:
        raise HTTPException(status_code=404,
                            detail=f"project not found: {project_id}")
    from shipin_platform.orchestration.pipeline_runner import _project_dir
    from shipin_platform.services.artifact_store import all_versions
    from shipin_platform.services import costing

    status = store.get_project_status(project_id)
    from shipin_platform.orchestration.stage_store import STAGES
    stages = []
    for name in STAGES:
        row = status.get(name)
        if row:
            stages.append({"stage": name,
                           "status": row.get("status"),
                           "artifact_hash": row.get("artifact_hash"),
                           "updated_at": row.get("updated_at")})
    events = store.list_events(project_id, limit=500)
    events.reverse()  # 旧 → 新
    proj_dir = _project_dir(project_id)
    assets = []
    media_ext = {".jpg", ".jpeg", ".png", ".webp",
                 ".mp4", ".webm", ".mov"}
    for p in sorted(proj_dir.glob("*")):
        if p.is_file() and p.suffix.lower() in media_ext:
            assets.append({
                "name": p.name,
                "kind": "video" if p.suffix.lower()
                in (".mp4", ".webm", ".mov") else "image",
                "bytes": p.stat().st_size})
    ver_idx = all_versions(proj_dir)
    return {
        "project_id": project_id,
        "stages": stages,
        "events": events,
        "assets": assets,
        "qc": store.list_clip_qc(project_id),
        "versions": {k: len(v) for k, v in ver_idx.items()},
        "cost": costing.cost_summary(project_id),
        "preview_frames": _preview_frame_urls(project_id),
    }


# ── Health ───────────────────────────────────────────────────────

@app.get("/api/projects")
def project_list(request: Request, limit: int = 50, mine: bool = False):
    """项目列表（新→旧）：前端项目页与外部 AI list_projects 共用。
    mine=true 时只列调用者自己的项目（owner=调用者标识）。"""
    owner = None
    principal = getattr(request.state, "principal", None)
    if mine:
        owner = (principal.caller if principal else None) or "default"
    return {"projects": _stage_store().list_projects(
        limit=min(max(limit, 1), 200), owner=owner)}


@app.get("/api/platform/integrity")
def platform_integrity():
    """平台封印自检：src/config/tools 与 config/integrity.json 清单比对。
    ok=false 表示平台核心已被改动（TAMPERED/MISSING/UNLISTED/SELF_TAMPERED）
    ——AI 修改平台级工具后这里立刻变红。仅暴露校验，无任何重建/解锁入口。"""
    from shipin_platform import integrity
    return integrity.verify()


@app.get("/api/health")
def health():
    """服务健康 + 关键凭据状态（只报有无，永不回显 key）。
    子 agent 判断『key 是否可用』必须以本端点为准，不要自己猜。"""
    agnes_key = os.environ.get("AGNES_KEY", "").strip()
    base = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
    return {
        "status": "ok",
        "version": app.version,
        "openmontage": OM_TOOLS_AVAILABLE,
        "agnes": {
            "key_configured": bool(agnes_key),
            "key_prefix": (agnes_key[:4] + "…") if agnes_key else "",
            "base_url": base,
            "video_modes": ["ti2vid", "keyframes(需 first_frame+last_frame)",
                            "multi_reference"],
            "video_models": ["agnes-video-2.5-flash(默认，reference 双图锚定)",
                             "agnes-video-v2.0(旧协议)"],
        },
        "gates": ["brief→script→storyboard→image_prompt→video_prompt 迭代审核",
                  "script/storyboard 语义审查(llm_review=true)",
                  "script/storyboard 用户确认闸门",
                  "agnes-video 首帧锚定强制(allow_unanchored=false)",
                  "/api/qc/clip 逐镜硬门(时长/镜内切/运动/首帧一致)",
                  "stitch 拒绝未经 QC 的镜头",
                  "final-video 双层终验(确定性结构+VLM 走查)"],
        "tools": {
            "ffmpeg": "available",
            "whisper": "available",
            "openmontage_stitch": OM_TOOLS_AVAILABLE,
            "openmontage_mixer": OM_TOOLS_AVAILABLE,
        },
        "auth": {
            "mode": auth_mode(),
            "admin_key_configured": bool(
                os.environ.get("SHIPIN_ADMIN_KEY", "").strip()),
        },
        "key_pool": _keypool_status(),
    }


# ── P0 平台级 API Key 管理（admin 专属）───────────────────────────────


class KeyIssueRequest(BaseModel):
    label: str
    scope: str = "read"          # read / write / admin
    project_id: Optional[str] = None  # 绑定项目后只能访问该项目


@app.post("/api/platform/keys")
def platform_keys_issue(req: KeyIssueRequest):
    """签发 API key。明文只在本次响应返回一次（库中仅存 sha256 指纹）。
    绑定 project_id 的 key 只能读写该项目（路径级强制）。"""
    if not req.label or not req.label.strip():
        raise HTTPException(status_code=422,
                            detail="label is required")
    try:
        plain, meta = _auth_store().issue(
            label=req.label.strip(), scope=req.scope,
            project_id=req.project_id)
    except Exception as e:  # AuthError 家族
        raise HTTPException(status_code=getattr(e, "status", 422),
                            detail=str(e))
    return {"ok": True, "key": plain, "once_only": True, **meta}


@app.get("/api/platform/keys")
def platform_keys_list():
    """列出全部未撤销 key 的记录（绝不回显明文）。"""
    return {"keys": _auth_store().list_keys()}


@app.delete("/api/platform/keys/{key_hash}")
def platform_keys_revoke(key_hash: str):
    """撤销 key（按 sha256 指纹；幂等）。"""
    ok = _auth_store().revoke(key_hash)
    raise_msg = "key not found or already revoked"
    if not ok:
        raise HTTPException(status_code=404, detail=raise_msg)
    return {"ok": True, "revoked": key_hash}


# ── key 池管理（生成商 key 以密文落库，AI 侧按 capability 从池中挑选）───


def _keypool_status() -> dict:
    """key 池只读状态：seal 是否配置、登记数量。永不包含任何 key 明文。"""
    seal = os.environ.get("SHIPIN_KEY_SEAL", "").strip()
    if len(seal) < 32:
        return {"seal_configured": False, "seal_min_bytes": 32, "count": None,
                "detail": "SHIPIN_KEY_SEAL 未配置或短于 32 字节——登记会被拒绝"}
    from shipin_platform.services.key_pool import get_pool
    try:
        rows = get_pool().list()
    except Exception as e:  # pragma: no cover
        return {"seal_configured": True, "count": None,
                "detail": f"key 池读取失败: {e}"}
    return {"seal_configured": True, "count": len(rows), "detail": "ok"}


class KeyPoolAddRequest(BaseModel):
    provider: str
    capability: str
    key: str
    service: str = ""
    priority: int = 0


class KeyPoolSetRequest(BaseModel):
    enabled: bool


@app.post("/api/platform/keypool")
def keypool_add(req: KeyPoolAddRequest):
    """登记生成 key（provider/capability 维度，如 agnes+video、agnes+image）。

    - 密钥只接受请求体 JSON 传入（不进 URL/日志），AES-256-GCM 密文落库；
    - 成功响应只回 id + sha256 前 8 位摘要，明文 key 绝不回显、绝不落日志。
    """
    from shipin_platform.services.key_pool import (
        SealMissingError as _SealMissingError, get_pool)
    try:
        rec = get_pool().add(
            provider=req.provider.strip(), capability=req.capability.strip(),
            key=req.key, service=req.service.strip(), priority=req.priority)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except _SealMissingError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=f"keypool add failed: {e}")
    return {"ok": True, "registered": rec}


@app.get("/api/platform/keypool")
def keypool_list(provider: str = "", capability: str = ""):
    """登记清单（脱敏：只返回 sha256 短摘要，无明文）。可按 provider/capability 过滤。"""
    from shipin_platform.services.key_pool import get_pool
    return {"keys": get_pool().list(provider=provider.strip(),
                                    capability=capability.strip())}


@app.patch("/api/platform/keypool/{key_id}")
def keypool_set(key_id: int, req: KeyPoolSetRequest):
    """启停某条登记（停用后 pick() 不再选中）。"""
    from shipin_platform.services.key_pool import get_pool
    ok = get_pool().set_enabled(key_id, req.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail=f"key not found: {key_id}")
    return {"ok": True, "id": key_id, "enabled": bool(req.enabled)}


@app.delete("/api/platform/keypool/{key_id}")
def keypool_remove(key_id: int):
    """删除某条登记（幂等：不存在返回 404）。"""
    from shipin_platform.services.key_pool import get_pool
    ok = get_pool().remove(key_id)
    raise_msg = f"key not found: {key_id}"
    if not ok:
        raise HTTPException(status_code=404, detail=raise_msg)
    return {"ok": True, "removed": key_id}


class GlobalBudgetRequest(BaseModel):
    max_monthly_usd: Optional[float] = None


@app.post("/api/platform/budget")
def platform_budget(req: GlobalBudgetRequest, request: Request):
    """全局月度预算（仅 admin key；middleware 已挡非 admin）。
    None/负值 → 解除全局硬闸。门槛：P3 FinOps 化。"""
    caller = getattr(getattr(request, "state", None), "principal", None)
    caller = (caller.caller if caller else "admin") or "admin"
    cfg = set_global_budget(req.max_monthly_usd, caller=caller)
    return {"ok": True, "global_budget": cfg}


@app.get("/api/platform/costs")
def platform_costs():
    """平台成本总览（仅 admin）：总额/本月/近 7 日 + top 项目（showback）。"""
    summary = global_cost_summary()
    exceeded, used, mx = global_budget_exceeded()
    summary["exceeded"] = exceeded
    if exceeded:
        summary["overrun_usd"] = round(used - mx, 6)
    return summary


@app.get("/api/platform/events")
@app.get("/api/platform/events.csv")
def platform_audit_events(limit: int = 500, request: Request = None):
    """请求审计导出（仅 admin；P7 OWASP API10）。

    默认返回 JSON `{events: [...]}`；路径/参数带 .csv 或 ?format=csv
    时返回 text/csv 全量（ts/caller/ip/method/route/status 等列）。
    中间件已强制 admin scope（/api/platform 前缀），此处不再重复校验。
    """
    max_rows = max(1, min(int(limit), 2000))
    ip = request.query_params.get("ip") if request else None
    caller = request.query_params.get("caller") if request else None
    rows = audit_store.list_all(limit=max_rows, ip=ip, caller=caller)
    if (request and (".csv" in request.url.path
                     or request.query_params.get("format") == "csv")):
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(
            audit_store.to_csv(rows), media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=events.csv"})
    return {"events": rows, "count": len(rows)}


# 节点画布（手操编排）路由——图 CRUD + 节点真实执行
from api_graph import router as _graph_router
app.include_router(_graph_router)

# 参考视频复刻路由——搜索/解析/下载/反推/注入画布
from api_reference import router as _reference_router
app.include_router(_reference_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8766)
