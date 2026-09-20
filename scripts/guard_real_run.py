# -*- coding: utf-8 -*-
"""scripts/guard_real_run.py — 真实 Agnes 链路的受控执行（先日志后调用）。

前置（都已验证）：
  1. SQLite 台账双写已就绪（tests/test_ledger_db_first.py 15/15）
  2. AGNES_KEY 连通（models 200）、ffmpeg / edge-tts / BGM 就绪

本脚本用 guard 驱动真实三步工作流（config/guard_workflow_video.json）：
  step1 intake_script   → pipeline.text   （Agnes LLM 写剧本/分镜 + 平台审查循环）
  step2 keyframes_video → pipeline.generate（真实生图→首尾帧链→生视频→QC→TTS）
  step3 post_production → pipeline.assemble（拼接→调色→字幕→声音→终验→发布）
每一步：call（真实调用，已自动落台账）→ request_review（审查门）→ 下一步。

用法：python scripts/guard_real_run.py [--project real-guard-1]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from shipin_platform.guard import (  # noqa: E402
    Guard, Ledger, WorkflowDefinition,
)
from shipin_platform.guard.adapters import build_registry  # noqa: E402
from shipin_platform.guard.ledger_db import LedgerDB  # noqa: E402

# 凭据装载：.env（含 api.py 同款约定）+ TEMP/agnes_key.txt 兜底（llm_review 同款）
from dotenv import load_dotenv  # noqa: E402
load_dotenv(ROOT / ".env")

PROJECT = "real-guard-1"
if "--project" in sys.argv:
    PROJECT = sys.argv[sys.argv.index("--project") + 1]

LEDGER_ROOT = ROOT / "data" / "guard_ledger"
BRIEF = {
    "content_type": "product",
    "product_info": "XX 冷萃咖啡：10 小时冷萃，0 蔗糖，清爽不苦，即刻购买可用 slogan：享受每一刻",
    "target_platform": "douyin",
    "duration_sec": 20,
    "target_audience": "都市白领/咖啡爱好者",
    "tone": "温暖治愈",
    "creative_direction": "清晨办公室，白领小憩，一杯冷萃带来片刻宁静；末镜品牌落版（XX咖啡 + slogan）",
    "reference_materials": "",
    "special_requirements": "落版必须出现 XX 咖啡品牌名与 slogan：享受每一刻；画面 16:9",
    "style_anchor": "cinematic, warm amber tones, soft natural light, 35mm film grain",
    "brand_name": "XX咖啡",
    "slogan": "享受每一刻",
}


def banner(t: str) -> None:
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


def main() -> int:
    registry = build_registry()
    wf = WorkflowDefinition.from_json_file(ROOT / "config" / "guard_workflow_video.json")
    ledger = Ledger(LEDGER_ROOT)
    guard = Guard(wf, registry, ledger)
    rid = guard.start(intent={"project_id": PROJECT, "brief": BRIEF})
    print(f"run_id = {rid}")
    print(f"workflow = {wf.name}（三步，每步审查门）")

    # ── Step 1：pipeline.text（真实 Agnes LLM 剧本/分镜 + 平台审查循环）──
    banner("STEP 1/3 pipeline.text  →  Agnes LLM 生成剧本/分镜（含平台审查循环）")
    r = guard.call("pipeline.text", {"project_id": PROJECT, "brief": BRIEF})
    ok1 = r.get("ok")
    print("pipeline.text ok:", ok1)
    if not ok1:
        print("失败原因：", json.dumps(r, ensure_ascii=False)[:600])
        print("→ 审查门提交（按实际产物决定 pass/revise）")
    sb = r.get("storyboard") or {}
    shots = sb.get("shots", [])
    print(f"分镜产出：{len(shots)} 镜；决策链：{[(s.get('stage'), s.get('decision')) for s in r.get('steps', [])]}")

    report1 = guard.request_review()
    print("审查门(step1)：", report1["verdict"], "—", report1["summary"][:120])
    if report1["verdict"] == "fail":
        print("台账：", rid, "——停在 step1，未进入生成阶段（按约束设计）")
        return 1

    # ── Step 2：pipeline.generate（真实生图→首尾帧→生视频→QC→TTS）──
    banner("STEP 2/3 pipeline.generate  →  真实生图 + 生视频 + 逐镜 QC + TTS")
    r2 = guard.call("pipeline.generate", {"project_id": PROJECT})
    print("pipeline.generate ok:", r2.get("ok"))
    for rep in (r2.get("report") or []):
        print("  镜", rep.get("shot_id"), "qc=", rep.get("qc"),
              "attempts=", len(rep.get("attempts", [])))
    align = r2.get("align") or {}
    print("对齐：", align.get("verdict"), "total=", align.get("total_sec"))

    report2 = guard.request_review()
    print("审查门(step2)：", report2["verdict"], "—", report2["summary"][:160])
    if report2["verdict"] == "fail":
        print("→ 停在 step2（素材不合格不进后期，符合『生图后必须审查』要求）")
        return 2

    # ── Step 3：pipeline.assemble（拼接→调色→字幕→声音→终验→发布）──
    banner("STEP 3/3 pipeline.assemble  →  真实后期合成 + VLM 终验")
    r3 = guard.call("pipeline.assemble", {"project_id": PROJECT})
    print("pipeline.assemble ok:", r3.get("ok"),
          "released:", r3.get("released"),
          "lufs:", r3.get("lufs"))
    fr = r3.get("final_review") or {}
    print("VLM 终验：", fr.get("verdict"), "brand_seen:", fr.get("brand_seen"))

    report3 = guard.request_review()
    print("审查门(step3)：", report3["verdict"], "—", report3["summary"][:160])
    st = guard.status()
    print("\n最终状态：", st["status"])
    print("final_path:", (r3 or {}).get("final_path"))
    return 0 if st["status"] == "completed" else 3


if __name__ == "__main__":
    raise SystemExit(main())
