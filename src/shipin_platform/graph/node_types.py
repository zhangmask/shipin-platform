"""节点类型注册表——节点工厂与「协议支持的参数」控制面。

每个节点定义：输入输出端口（端口类型 = 数据类型，连线按类型校验）、
参数规格、执行签名。video_gen 的参数视图随模型变化：
  - agnes-video-2.5-flash（默认）：reference 双图锚定；duration 固定 5s
    （服务端不接受 duration/resolution 字段）→ 参数列表里 duration 为
    只读 5s、resolution 只读 720p、无 negative_prompt。
  - agnes-video-v2.0（旧协议）：keyframes、可设 duration/resolution/
    negative_prompt。
前端直接消费 ``definitions_json()`` 渲染表单；``effective_params()``
负责按模型过滤出真正会发送的字段（不显示死参数）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# ---- 端口/参数类型常量 -------------------------------------------------
P_TEXT = "text"        # 自由文本
P_SELECT = "select"    # 枚举下拉
P_NUM = "number"       # 数字
P_SLIDER = "slider"    # 滑块（携带 min/max/step）
P_BOOL = "bool"
P_PATH = "path"        # 已有文件路径

KIND_TEXT = "text"
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_REPORT = "report"

# 模型名单（与服务端 /v1/models 实测一致；供没有网络的环境兜底）
VIDEO_MODELS = [
    {"value": "agnes-video-2.5-flash",
     "label": "agnes-video-2.5-flash（默认，reference 双图锚定）"},
    {"value": "agnes-video-v2.0",
     "label": "agnes-video-v2.0（旧协议 keyframes，可调速时长）"},
]
IMAGE_MODELS = [
    {"value": "agnes-image-2.0-flash", "label": "agnes-image-2.0-flash"},
    {"value": "agnes-image-2.1-flash", "label": "agnes-image-2.1-flash"},
    {"value": "agnes-image-2.5-flash", "label": "agnes-image-2.5-flash"},
    {"value": "pil", "label": "本地 PIL 占位图（不花钱）"},
]

# 2.5 协议语义：模型 → 参数视图
_V25_MODELS = {"agnes-video-2.5-flash", "agnes-video-2.5"}


def video_param_view(model: str) -> dict:
    """按所选模型返回 video_gen 的参数 schema（协议感知）。"""
    if model in _V25_MODELS:
        return {
            "duration": {"kind": P_NUM, "label": "时长(秒)",
                         "value": 5, "min": 5, "max": 5, "readonly": True,
                         "hint": "2.5 协议固定 5s，服务端不接受 duration——多镜头拼接可延片"},
            "resolution": {"kind": P_SELECT, "label": "分辨率",
                           "readonly": True, "value": "720p",
                           "options": [{"value": "720p", "label": "720p（协议锁定）"}]},
        }
    return {
        "duration": {"type": P_NUM, "label": "时长(秒)",
                     "value": 5, "min": 2, "max": 10},
        "resolution": {"type": P_SELECT, "label": "分辨率",
                       "value": "720p",
                       "options": [{"value": "720p", "label": "720p"},
                                   {"value": "1080p", "label": "1080p"}]},
        "negative_prompt": {"type": P_TEXT, "label": "负面提示词",
                            "value": "", "hint": "仅旧协议支持"},
    }


@dataclass
class PortDef:
    name: str
    kind: str
    label: str
    required: bool = False


@dataclass
class ParamDef:
    key: str
    label: str
    schema: dict            # {type, value, options?, min?, max?...}
    dynamic: Optional[Callable[[str], dict]] = None  # 依模型变化的参数视图


@dataclass
class NodeTypeDef:
    key: str
    label: str
    category: str
    color: str              # 画布配色
    inputs: list[PortDef]
    outputs: list[PortDef]
    params: list[ParamDef]
    hint: str = ""
    executor: Optional[str] = None   # engine.executors 里的 key（None=纯计算）

    def params_json(self, model: str = "") -> list[dict]:
        out = []
        for p in self.params:
            sch = p.schema
            if p.dynamic and model:
                sch = dict(sch)
                sch.update(p.dynamic(model))
            out.append({"key": p.key, "label": p.label, "schema": sch})
        return out


def _mk(name: str, label: str, cat: str, color: str,
        inputs: list, outputs: list, params: list, hint: str = "",
        executor: str = "") -> NodeTypeDef:
    return NodeTypeDef(
        key=name, label=label, category=cat, color=color,
        inputs=[PortDef(**i) for i in inputs],
        outputs=[PortDef(**o) for o in outputs],
        params=[ParamDef(**p) for p in params], hint=hint, executor=executor)


REGISTRY: dict[str, NodeTypeDef] = {
    "text": _mk(
        "text", "文本 / 提示词", "基础", "#64748b",
        [], [{"name": "text", "kind": KIND_TEXT, "label": "文本"}],
        [{"key": "text", "label": "内容",
          "schema": {"type": P_TEXT, "value": "",
                     "hint": "可直接输入，也可不填由上游连线提供"}}],
        hint="提示词/旁白字稿源"),
    "image_gen": _mk(
        "image_gen", "生成首帧图片", "生成", "#8b5cf6",
        [{"name": "prompt", "kind": KIND_TEXT, "label": "提示词"}],
        [{"name": "image", "kind": KIND_IMAGE, "label": "图片"}],
        [{"key": "model", "label": "图片模型",
          "schema": {"type": P_SELECT, "value": "agnes-image-2.1-flash",
                     "options": IMAGE_MODELS}},
         {"key": "prompt", "label": "提示词",
          "schema": {"type": P_TEXT, "value": "",
                     "hint": "与上游文本输入二选一（连线优先）"}},
         {"key": "width", "label": "宽", "schema": {"type": P_NUM, "value": 1280, "min": 256}},
         {"key": "height", "label": "高", "schema": {"type": P_NUM, "value": 720, "min": 256}}],
        hint="文生图：输出可作为 video_gen 的首帧/尾帧连线"),
    "video_gen": _mk(
        "video_gen", "视频生成（锚定）", "生成", "#3b82f6",
        [{"name": "prompt", "kind": KIND_TEXT, "label": "提示词", "required": True},
         {"name": "first_frame", "kind": KIND_IMAGE, "label": "首帧图",
          "required": True},
         {"name": "last_frame", "kind": KIND_IMAGE, "label": "尾帧图（可选）"}],
        [{"name": "video", "kind": KIND_VIDEO, "label": "视频"}],
        [{"key": "model", "label": "视频模型",
          "schema": {"type": P_SELECT, "value": VIDEO_MODELS[0]["value"],
                     "options": VIDEO_MODELS}},
         {"key": "prompt", "label": "提示词",
          "schema": {"type": P_TEXT, "value": "", "hint": "与上游则连线二选一"}},
         {"key": "duration", "label": "镜头时长",
          "schema": {"type": P_NUM, "value": 5, "min": 2, "max": 10}},
         {"key": "resolution", "label": "分辨率",
          "schema": {"type": P_SELECT, "value": "720p",
                     "options": [{"value": "720p", "label": "720p"}]}}],
        hint="输出 5s 镜头；多镜头连线到拼接节点即延片"),
    "tts": _mk(
        "tts", "配音 TTS", "音频", "#10b981",
        [{"name": "text", "kind": KIND_TEXT, "label": "配音文案", "required": True}],
        [{"name": "audio", "kind": KIND_AUDIO, "label": "音频"}],
        [{"key": "text", "label": "文案",
          "schema": {"type": P_TEXT, "value": "", "hint": "留空则取上游文本"}},
         {"key": "role", "label": "音色",
          "schema": {"type": P_SELECT, "value": "biz_female",
                     "options": [{"value": "biz_female", "label": "商务女声"},
                                 {"value": "biz_male", "label": "商务男声"},
                                 {"value": "crisp_female", "label": "清脆女声"}]}}],
        hint="旁白配音；输出音频供拼接节点混音"),
    "qc": _mk(
        "qc", "逐镜质检 QC", "质检", "#f59e0b",
        [{"name": "video", "kind": KIND_VIDEO, "label": "视频", "required": True}],
        [{"name": "report", "kind": KIND_REPORT, "label": "质检报告"}],
        [{"key": "expected_duration", "label": "期望时长(sec)",
          "schema": {"type": P_NUM, "value": 5, "min": 1}},
         {"key": "max_internal_cuts", "label": "允许镜内切数",
          "schema": {"type": P_NUM, "value": 0, "min": 0}},
         {"key": "check_motion", "label": "检查运动量",
          "schema": {"type": P_BOOL, "value": True}}],
        hint="硬门：时长/内切/运动/首帧一致；verdict=ok 才进拼接"),
    "assemble": _mk(
        "assemble", "拼接出片", "交付", "#ef4444",
        [{"name": "clips", "kind": KIND_VIDEO, "label": "镜头(可多台)", "required": True}],
        [{"name": "final", "kind": KIND_VIDEO, "label": "成片"}],
        [{"key": "fps", "label": "帧率", "schema": {"type": P_NUM, "value": 24, "min": 12}},
         {"key": "color_grade", "label": "商业温暖调色",
          "schema": {"type": P_BOOL, "value": True}},
         {"key": "burn_audio", "label": "混入配音",
          "schema": {"type": P_BOOL, "value": True}}],
        hint="按连线顺序串联多镜头 → 单个成片（时长 = 各镜之和）"),

    # ---------------- 阶段流水线（AI 驱动：上游 AI 写入 → 用户确认 → 逐级下推）----
    # 与生成节点不同，这些是「编排节点」：内容由外部 AI（Codex 等）经 API 写入，
    # 画布只是让每一步立即可见、可改、可审核；执行时把内容物化到 artifacts。
    "script": _mk(
        "script", "剧本/文案", "阶段①策划", "#f472b6",
        [{"name": "brief", "kind": KIND_TEXT, "label": "简报/需求"}],
        [{"name": "progress", "kind": KIND_TEXT, "label": "剧本"}],
        [{"key": "content", "label": "剧本正文(每镜一行，含镜头/台词/时长)",
          "schema": {"type": P_TEXT, "value": "",
                     "hint": "AI 写入或手工粘贴；每镜格式：[镜头描述] 台词 | 参考时码"}}],
        hint="阶段：策划。上游简报/需求（text）→ 剧本文本"),
    "storyboard": _mk(
        "storyboard", "分镜", "stage 生成", "#8b5cf6",
        [{"name": "script", "kind": KIND_TEXT, "label": "剧本"}],
        [{"name": "board", "kind": KIND_TEXT, "label": "分镜脚本"}],
        [{"key": "content", "label": "分镜(每镜: 镜头/景别/内容/时长)",
          "schema": {"type": P_TEXT, "value": "",
                     "hint": "AI 按剧本展开为可执行分镜；用户可改写后重跑"}}],
        "阶段：② 分镜。把剧本扩展为逐镜可执行分镜"),
    "frame_prompts": _mk(
        "frame_prompts", "首尾帧生图提示词", "stage_prompt", "#3b82f6",
        [{"name": "board", "kind": KIND_TEXT, "label": "分镜"}],
        [{"name": "first_prompt", "kind": KIND_TEXT, "label": "首帧提示词"},
         {"name": "last_prompt", "kind": KIND_TEXT, "label": "尾帧提示词"}],
        [{"key": "first_prompt", "label": "首帧提示词",
          "schema": {"type": P_TEXT, "value": "", "hint": "描述首帧构图/主体/景别；供 image_gen 出首帧"}},
         {"key": "last_prompt", "label": "尾帧提示词",
          "schema": {"type": P_TEXT, "value": "", "hint": "可选：相同构图+收尾姿态，实现首尾帧锚定"}}],
        "阶段：③ 生图提示词。给每个镜头的首帧/尾帧的精确提示词"),
    "video_prompt": _mk(
        "video_prompt", "视频提示词", "stage_prompt", "#3b82f6",
        [{"name": "board", "kind": KIND_TEXT, "label": "分镜（可选）"}],
        [{"name": "prompt", "kind": KIND_TEXT, "label": "视频提示词"}],
        [{"key": "content", "label": "视频提示词(描述镜头内运动)",
          "schema": {"type": P_TEXT, "value": "", "hint": "人物动作/镜头语言/氛围；拟对 to video_gen"}}],
        "阶段：③ 视频提示词。描述镜头内运动，进入 video_gen"),
    "review": _mk(
        "review", "审核门", "stage_damn", "#f59e0b",
        [{"name": "target", "kind": KIND_TEXT, "label": "待审内容（或多物化）"}],
        [{"name": "verdict", "kind": KIND_REPORT, "label": "审核记录"},
         {"name": "content", "kind": KIND_TEXT, "label": "通过的内容（转发）"},
         {"name": "suggestion", "kind": KIND_TEXT, "label": "AI 修改建议"}],
        [{"key": "display_name", "label": "节点名",
          "schema": {"type": P_TEXT, "value": "", "hint": "如「剧本复核」「首帧图复核」"}},
         {"key": "stage", "label": "关联阶段",
          "schema": {"type": P_SELECT, "value": "scene_script",
                     "options": [{"value": "script", "label": "剧本"},
                                 {"value": "storyboard", "label": "分镜"},
                                 {"value": "frame_image", "label": "首尾帧图"},
                                 {"value": "video", "label": "视频"},
                                 {"value": "final", "label": "终检"}]}},
         {"key": "status", "label": "审核状态",
          "schema": {"type": P_SELECT, "value": "pending",
                     "options": [{"value": "pending", "label": "待审核"},
                                 {"value": "pass", "label": "通过 ✅"},
                                 {"value": "reject", "label": "驳回 ❌"}]}},
         {"key": "auto_review", "label": "自动审查",
          "schema": {"type": P_SELECT, "value": "manual",
                     "options": [{"value": "manual", "label": "人工判定"},
                                 {"value": "auto", "label": "AI 自动审查内容"}]}},
         {"key": "comments", "label": "审核意见",
          "schema": {"type": P_TEXT, "value": "", "hint": "驳回原因/修改要求"}}],
        "阶段：🛡 审核门。AI 自动审查（auto_review=AI）或人工判定（自动落盘留痕）；未通过（pending/reject）会拦截下游运行"),
    "card": _mk(
        "card", "成品卡片", "stage_delivery", "#ec4899",
        [{"name": "final_video", "kind": KIND_VIDEO, "label": "成片"}],
        [{"name": "card", "kind": KIND_REPORT, "label": "成品卡"}],
        [{"key": "title", "label": "标题", "schema": {"type": P_TEXT, "value": "",
                                                     "hint": "成片标题（卡片首行）"}},
         {"key": "subtitle", "label": "副标题", "schema": {"type": P_TEXT, "value": "",
                                                        "hint": "一句话卖"}},
         {"key": "tags", "label": "标签(逗号分隔)",
          "schema": {"type": P_TEXT, "value": "", "hint": "商业/美食/情感…"}}],
        "阶段：交付。把成片 + 标题 + 标签整理成可供上架的成品卡片"),
}


def definitions_json() -> dict:
    """前端画布消费的节点类型定义（含全部参数 schema）。"""
    return {
        key: {
            "key": d.key, "label": d.label, "category": d.category,
            "color": d.color, "hint": d.hint,
            "inputs": [p.__dict__ for p in d.inputs],
            "outputs": [p.__dict__ for p in d.outputs],
            "params": d.params_json(),
        }
        for key, d in REGISTRY.items()
    }


def effective_params(def_: NodeTypeDef, params: dict, model: str = "") -> dict:
    """按协议过滤出真正会发送给执行器的参数（2.5 锁 duration/resolution）。"""
    out = dict(params)
    if def_.key == "video_gen" and model:
        view = _V25_MODELS and model in _V25_MODELS
        if view:
            out["duration"] = 5
            out["resolution"] = "720p"
            out.pop("negative_prompt", None)
    return out


def get_def(key: str) -> NodeTypeDef:
    d = REGISTRY.get(key)
    if not d:
        raise KeyError(f"unknown node type: {key}")
    return d