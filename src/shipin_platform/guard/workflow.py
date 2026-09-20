"""agent_guard.workflow — 步骤编排配置（数据驱动，非代码驱动）。

一个 WorkflowDefinition = 有序步骤列表；每个步骤声明：
  - actions: 该步骤内 Agent 允许调用的平台函数（子集白名单）
  - entry_actions: 进入本步骤时由编排器自动执行的函数（可为空）
  - reviewer: 步骤完成后的审查函数（审查门）
  - review_focus: 审查提示（传给审查函数的说明）
  - auto_advance: 审查通过后是否自动进入下一步（默认 True）
  - manual_pass_enabled: 人工放行开关（默认 False）

工作流用 JSON/YAML/Python dict 定义均可；这里提供
from_dict/from_json_file 与内置的 VIDEO_PIPELINE（六步视频流水线示例）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import WorkflowValidationError
from .registry import ACTION, Registry

REQUIRED_KEYS = {"name", "intent_schema", "steps"}
REQUIRED_STEP_KEYS = {"name", "actions", "reviewer"}


def _validate_wf(data: Dict[str, Any], registry: Optional[Registry] = None) -> None:
    missing = REQUIRED_KEYS - set(data)
    if missing:
        raise WorkflowValidationError(f"工作流缺少必填字段：{sorted(missing)}")
    steps = data["steps"]
    if not isinstance(steps, list) or not steps:
        raise WorkflowValidationError("steps 必须是非空列表")
    names = set()
    for i, step in enumerate(steps, 1):
        miss = REQUIRED_STEP_KEYS - set(step)
        if miss:
            raise WorkflowValidationError(
                f"第 {i} 个步骤缺少必填字段：{sorted(miss)}")
        if step["name"] in names:
            raise WorkflowValidationError(f"步骤名重复：{step['name']}")
        names.add(step["name"])
    if registry is not None:
        _validate_wf_registry(data, registry)


def _validate_wf_registry(data: Dict[str, Any], registry: Registry) -> None:
    """工作流 ↔ 注册表一致性校验（在 Guard 初始化时执行一次）。"""
    for i, step in enumerate(data["steps"], 1):
        for fn_name in step.get("entry_actions", []):
            if not registry.has(fn_name):
                raise WorkflowValidationError(
                    f"步骤「{step['name']}」的 entry_actions 引用了未注册函数「{fn_name}」")
            rf = registry.get(fn_name)
            if rf.category != ACTION:
                raise WorkflowValidationError(
                    f"步骤「{step['name']}」的 entry_actions「{fn_name}」是审查函数，"
                    f"不能作为动作执行")
        for fn_name in step["actions"]:
            if not registry.has(fn_name):
                raise WorkflowValidationError(
                    f"步骤「{step['name']}」的 actions 引用了未注册函数「{fn_name}」")
            rf = registry.get(fn_name)
            if rf.category != ACTION:
                raise WorkflowValidationError(
                    f"步骤「{step['name']}」的 actions「{fn_name}」是审查函数，"
                    f"Agent 不可直接调用审查函数；审查由审查门自动触发")
        rev = step["reviewer"]
        if not registry.has(rev):
            raise WorkflowValidationError(
                f"步骤「{step['name']}」的 reviewer「{rev}」未注册")
        if registry.get(rev).category != "reviewer":
            raise WorkflowValidationError(
                f"步骤「{step['name']}」的 reviewer「{rev}」不是审查函数"
                f"（注册时请用 register_reviewer）")


class WorkflowDefinition:
    """校验后的工作流定义。"""

    def __init__(self, data: Dict[str, Any]) -> None:
        _validate_wf(data)
        self._data = data

    # ── 构造 ────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorkflowDefinition":
        return cls(data)

    @classmethod
    def from_json_file(cls, path: "str | Path") -> "WorkflowDefinition":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data)

    # ── 访问 ────────────────────────────────────────────────
    @property
    def name(self) -> str:
        return self._data["name"]

    @property
    def intent_schema(self) -> Dict[str, Any]:
        return self._data.get("intent_schema", {})

    @property
    def steps(self) -> List[Dict[str, Any]]:
        return self._data["steps"]

    def step(self, index: int) -> Dict[str, Any]:
        return self._data["steps"][index]

    def step_index(self, name: str) -> int:
        for i, s in enumerate(self._data["steps"]):
            if s["name"] == name:
                return i
        raise WorkflowValidationError(f"工作流中没有步骤「{name}」")

    def to_dict(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._data, ensure_ascii=False))


# ── 内置示例：六步视频生产流水线 ────────────────────────────
# 步骤与审查门设计（对应 Goal Brief 的工作流）：
#   1 intake_script      用户意图澄清 + 粗略剧本
#   2 script_refine      剧本细化
#   3 storyboard         剧本 → 分镜
#   4 keyframes          分镜 → 首尾帧提示词 → 生成首尾帧
#   5 video_gen          首尾帧 + 视频提示词 → 视频段
#   6 post_production    剪辑 + 配音 + 音乐 + 特效
VIDEO_PIPELINE: Dict[str, Any] = {
    "name": "video_pipeline_v1",
    "intent_schema": {
        "fields": ["raw_input", "material_files", "idea"],
        "description": "用户给的一段文字 / 上传的素材 / 想法",
    },
    "steps": [
        {
            "name": "intake_script",
            "title": "需求澄清与粗剧本",
            "actions": ["clarify_intent", "draft_rough_script"],
            "entry_actions": ["ingest_user_input"],
            "reviewer": "review_rough_script",
            "review_focus": "粗剧本是否忠实于用户需求；题材/时长/受众是否明确",
            "auto_advance": True,
            "manual_pass_enabled": True,
        },
        {
            "name": "script_refine",
            "title": "剧本细化",
            "actions": ["refine_script"],
            "reviewer": "review_script",
            "review_focus": "剧本结构完整性、逐镜可拍性、旁白字数与时长匹配",
            "auto_advance": True,
            "manual_pass_enabled": True,
        },
        {
            "name": "storyboard",
            "title": "分镜制作",
            "actions": ["make_storyboard"],
            "reviewer": "review_storyboard",
            "review_focus": "镜头要素齐全（景别/运动/场景/首尾动作）、相邻镜头衔接可拍",
            "auto_advance": True,
            "manual_pass_enabled": True,
        },
        {
            "name": "keyframes",
            "title": "首尾帧生成",
            "actions": ["write_frame_prompts", "generate_keyframes"],
            "reviewer": "review_keyframes",
            "review_focus": "图片本身质量 + 与该镜头在成片中是否合规（主体一致性/构图/风格统一/无畸变）",
            "auto_advance": True,
            "manual_pass_enabled": True,
        },
        {
            "name": "video_gen",
            "title": "视频生成",
            "actions": ["write_video_prompts", "generate_video"],
            "reviewer": "review_video_clips",
            "review_focus": "视频段质量 + 在成片语境中是否合规（运动连续/无内切/首尾帧匹配/时长准确）",
            "auto_advance": True,
            "manual_pass_enabled": True,
        },
        {
            "name": "post_production",
            "title": "后期制作",
            "actions": ["edit_timeline", "add_voiceover", "add_music", "add_effects", "render_final"],
            "reviewer": "review_final_film",
            "review_focus": "成片整体：剪辑节奏/音画同步/响度达标/品牌元素/黑帧",
            "auto_advance": True,
            "manual_pass_enabled": True,
        },
    ],
}
