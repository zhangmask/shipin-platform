# shipin-platform · guard 受控执行机制 — 使用与交接文档

> 面向对象：接手本模块的工程师 / 配置流水线的产品 / 被约束的 AI Agent 本身。
> 位置：`D:\aishipin\shipin-platform\src\shipin_platform\guard\`（正式项目内，随包安装）。

---

## 1. 它解决什么问题

Agent 自由编排时会"走偏"：跳过审查、调错函数、带着不合格素材往下走。
guard 把三件事变成硬约束（代码强制，不靠提示词自觉）：

1. **白名单**——Agent 只能调用注册表里的平台函数；清单外的调用直接拒绝
   （`UNKNOWN_FUNCTION`），不产生任何执行，拒绝本身记入台账。
2. **顺序**——工作流定义固定步骤序列；当前步骤没走完，调下一步函数被阻断
   （`NOT_ALLOWED_IN_STEP`），错误信息告诉你现在第几步、能调什么。
3. **审查门**——每步完成后必须 `request_review()`，通过才自动进入下一步；
   不通过停留+原因；连败达上限运行变 `blocked`，一切调用被拒，
   只有人工放行（该步骤开启时）能解锁。
4. **台账**——调用/拒绝/审查/推进全程 JSONL 落盘，按运行可查询、成败可区分。

## 2. 与平台既有代码的关系（只包装，不重写）

```
Agent ──call/request_review──▶ guard（约束壳）
                                  │ adapters.py 只做委托：
                                  ├─ pipeline.text     → run_text_phase     （既有）
                                  ├─ pipeline.generate → run_generate_phase （既有）
                                  ├─ pipeline.assemble → run_assemble_phase （既有）
                                  ├─ gen.image/video   → generate_image/video_agnes（既有）
                                  └─ review.*          → qc_clip / vlm_review_final / 平台审查结论（既有）
```

- `adapters.py` 是唯一"接线层"：没有一行生成/审查逻辑，全部委托既有实现。
  内置自检用例 `test_real_adapters_delegate_not_duplicate` 保证这一点不回退。
- `demo_functions.py` 是**演示专用**模拟件（不触网），仅用于约束逻辑的测试与教学。
- 两套工作流配置：
  - `config/guard_workflow_video.json` — 真实链路（pipeline.text/generate/assemble 三步，各挂审查门）
  - `scripts/guard_demo.py` 内联 DEMO_WF — 演示六步（模拟函数）

## 3. 快速上手

```bash
cd D:\aishipin\shipin-platform
pip install -e .                        # 修复过 build-backend，现可正常安装
python tests\test_guard_constraints.py  # 13 个约束用例（含真实装配冒烟）
python scripts\guard_demo.py            # 四场景演示（模拟，不触网）
python src\shipin_platform\guard\ledger.py data\guard_ledger_demo --list
```

正式接入（真实函数）：

```python
from shipin_platform.guard import build_real_registry, Guard, Ledger, WorkflowDefinition

registry = build_real_registry()
wf = WorkflowDefinition.from_json_file("config/guard_workflow_video.json")
guard = Guard(wf, registry, Ledger("data/guard_ledger"))
guard.start(intent={"project_id": "coffee-v7", "brief": {...}})
guard.call("pipeline.text", {"project_id": "coffee-v7"})   # 白名单+顺序校验后委托真实实现
guard.request_review()   # 复用平台审查结论做门禁；通过才进下一步
guard.status()
```

## 4. 注册新的平台函数 / 审查函数

```python
from shipin_platform.guard import Registry

def gen_voiceover(ctx=None, script_path="", out="", **_kw):
    """ctx 自动注入：intent + 各步产物。可内部调用任何既有模块。"""
    from shipin_platform.services.tts_service import create_tts_service
    ...
    return {"voice": out}

reg = Registry()
reg.register("gen.voiceover", gen_voiceover, description="配音", params_hint="script_path, out")
reg.register_reviewer("review_voiceover", review_fn)   # 签名 fn(artifacts, context) -> verdict/rules/summary
```

要点：
- 引擎按函数签名过滤参数（多余参数忽略），声明 `ctx` 形参即拿运行时数据流；
- 审查函数返回契约：`{"verdict": "pass"|"fail", "rules": [{rule, dimension,
  passed, detail}], "summary"}`；`dimension` 二选一：
  `artifact_quality`（素材本身）/ `in_video_compliance`（放进成片是否合规，用户最看重）；
- 注册表是白名单唯一来源：没注册 = 对 Agent 不存在。

## 5. 增改步骤与审查规则

编辑 `config/guard_workflow_video.json`（或另起新 JSON）：

```jsonc
{ "name": "...", "steps": [{
    "name": "voiceover", "title": "配音",
    "actions": ["gen.voiceover"],          // 本步白名单（必须已注册）
    "reviewer": "review_voiceover",        // 审查门
    "manual_pass_enabled": true,           // 人工放行开关
    "max_review_attempts": 3               // 连败阻断上限
}]}
```

- 配置引用未注册函数 → Guard 初始化即抛 `WORKFLOW_INVALID`（fail-fast）；
- 审查阈值建议从 `context["params"]` 读（步骤里配 `review_params`），
  业务标准变化只改配置；
- 人工放行 `guard.manual_pass(reason, approver)`：该步骤开启开关 + 已至少
  一次自动审查失败才可用；blocked 时是唯一解锁通道；每次放行独立留痕。

## 6. 排查手册（错误码 → 处置）

| 错误码 | 含义 | 处置 |
|---|---|---|
| `UNKNOWN_FUNCTION` | 调了未注册函数 | 检查拼写或按 §4 注册 |
| `NOT_ALLOWED_IN_STEP` | 跳步/乱序 | 看消息里"本步骤允许调用"，按顺序推进 |
| `REVIEWER_NOT_CALLABLE` | Agent 直调审查函数 | 审查由 `request_review()` 自动触发 |
| `STEP_NOT_READY` | 未 start / 已完成 / blocked / 无产物 | 按消息操作；blocked 走人工放行 |
| `MANUAL_PASS_DISABLED` | 步骤未开人工放行 | 开开关或走"修复重审" |
| `PLATFORM_FUNCTION_FAILED` | 平台函数自身异常 | 看台账 `function_failed` 事件 |
| `WORKFLOW_INVALID` | 配置与注册表不一致 | 按消息补注册/改配置 |

台账查询：

```bash
python src\shipin_platform\guard\ledger.py data\guard_ledger_demo --list
python src\shipin_platform\guard\ledger.py data\guard_ledger_demo --run RUN-XXXX
```

## 7. 测试与质量门

- `tests/test_guard_constraints.py` — 13 用例：约束逻辑（demo 双轨，不触网）
  + 真实装配冒烟（`build_real_registry` ↔ `guard_workflow_video.json` 对齐、
  adapters 只包装不重写的源码级自检）。
- 全套件 `pytest tests -q` 当前 **130 passed**（含本模块 13 个）。
- 本次顺带修复的既有问题（与 guard 无关但影响安装/测试）：
  pyproject 的 `build-backend` 指向不存在的模块 → 改为 `setuptools.build_meta`；
  whisper 可选依赖被顶层硬 import → 改懒加载（`api.py`/`main.py`/`tools/__init__.py`），
  whisper 缺失时仅转写相关端点返回 503，其余功能不受影响。

## 8. 边界

- 单 Agent 顺序执行；多 Agent 并发抢占不在本版范围；
- 审查函数内部实现（规则/LLM/VLM）不受本模块约束，本模块只管
  "何时必须审、结论如何阻断流程"；
- 台账为本地 JSONL 文件，生产部署时如需集中式台账可替换 `Ledger` 实现（接口不变）。
