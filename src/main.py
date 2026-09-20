"""shipin-platform — AI Video Generation Platform entry point."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Ensure package is importable
sys.path.insert(0, str(Path(__file__).parent / "src"))

from shipin_platform import FFmpegEngine, ReviewEngine

# whisper 为可选依赖；缺失时仅 /review 之外的转写相关 CLI 不可用
try:
    from shipin_platform.tools import WhisperService  # noqa: F401
except ImportError:
    WhisperService = None
from shipin_platform.review import score_slideshow_risk, check_scene_variation, Decision


def cmd_review(args):
    """Run review on a JSON input, iterating with mechanical fixes.

    Mirrors /api/review/iterate: each round runs the review engine; if the
    decision is REVISE and mechanical fixes apply (style, subjective
    words, camera terms, I2V appearance cleanup), they are applied to a copy
    of the data and the next round reviews the repaired data. Findings without a
    mechanical fix land in `manual_modes` for the caller (LLM/agent) to
    regenerate content.
    """
    with open(args.input, encoding="utf-8") as f:
        data = json.load(f)

    engine = ReviewEngine({"max_rounds": {args.stage: args.rounds}})
    rounds = []
    manual = {}
    data_running = data
    prev_report = None
    fix_applied = False
    final = None

    for round_num in range(1, args.rounds + 1):
        final = engine.run_review(
            args.stage, data_running, round_num=round_num,
            previous_report=prev_report, fix_applied=fix_applied,
        )
        rounds.append(final.to_dict())
        if final.decision in (Decision.PASS, Decision.PASS_WITH_WARNINGS,
                              Decision.STALL, Decision.STOP):
            break
        fix_result = engine.revision.fix(args.stage, data_running, final)
        data_running = fix_result["data"]
        fix_applied = bool(fix_result["applied"])
        if fix_result["manual"]:
            manual[f"r{round_num}"] = fix_result["manual"]
        prev_report = final

    out = {
        "stage": args.stage,
        "decision": final.decision.value,
        "rounds_run": len(rounds),
        "rounds": rounds,
        "data": data_running,
        "manual_modes": manual,
        "next_action": final.next_action,
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if final.decision in (Decision.PASS, Decision.PASS_WITH_WARNINGS) else 1


def cmd_stitch(args):
    """Stitch video clips together."""
    engine = FFmpegEngine()
    result = engine.xfade_chain(
        [Path(c) for c in args.clips],
        Path(args.output),
        transition=args.transition,
        duration=args.duration,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_subtitle(args):
    """Generate and burn subtitles."""
    # Step 1: Transcribe with Whisper
    ws = WhisperService(model="base")
    transcribe_result = ws.transcribe(Path(args.audio))
    srt_path = Path(transcribe_result["srt_path"])
    print(f"Subtitle generated: {srt_path}")

    # Step 2: Burn subtitles
    engine = FFmpegEngine()
    result = engine.burn_srt(
        Path(args.video), srt_path, Path(args.output),
        font_name=args.font, font_size=args.size,
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_analyze(args):
    """Analyze video quality."""
    engine = FFmpegEngine()
    probe = engine.probe(Path(args.video))
    result = {
        "codec": probe.codec,
        "resolution": f"{probe.width}x{probe.height}",
        "fps": probe.fps,
        "duration_sec": probe.duration,
        "audio": probe.audio_codec,
    }
    if args.black_detect:
        black = engine.black_detect(Path(args.video))
        result["black_frames"] = black

    print(json.dumps(result, indent=2))
    return 0


def cmd_pipeline(args):
    """Deprecated: 全链路已收敛到平台 API（受控编排），CLI 不再执行任何工作。"""
    print("`` shipin pipeline`` 已废弃：旧 review-only 流水线已被平台受控编排取代。\n"
          "   请通过平台 API 使用：POST /api/pipeline/text {project_id, brief}\n"
          "   之后依次调用 /api/project/confirm → /api/pipeline/generate →\n"
          "   /api/pipeline/assemble（完整契约见 GET /api/agent-guide）。",
          file=sys.stderr)
    return 2


def main():
    parser = argparse.ArgumentParser(prog="shipin",
                                     description="AI Video Generation Platform")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("review", help="Review content at a stage")
    p.add_argument("input", help="JSON file with content to review")
    p.add_argument("--stage", "-s", default="brief",
                   choices=["brief", "script", "storyboard",
                            "image_prompt", "video_prompt"])
    p.add_argument("--rounds", "-r", type=int, default=3)

    p = sub.add_parser("stitch", help="Stitch video clips")
    p.add_argument("--clips", nargs="+", required=True, help="Input clips")
    p.add_argument("--output", "-o", required=True, help="Output path")
    p.add_argument("--transition", "-t", default="fade",
                   choices=["cut", "crossfade", "fade"],
                   help="cut = hard cut, crossfade = cross dissolve, "
                        "fade = fade through black")
    p.add_argument("--duration", "-d", type=float, default=0.8)

    p = sub.add_parser("subtitle", help="Generate and burn subtitles")
    p.add_argument("--audio", "-a", required=True, help="Audio file")
    p.add_argument("--video", "-v", required=True, help="Video file")
    p.add_argument("--output", "-o", required=True, help="Output path")
    p.add_argument("--font", default="Microsoft YaHei")
    p.add_argument("--size", type=int, default=18)

    p = sub.add_parser("analyze", help="Analyze video quality")
    p.add_argument("video", help="Video file to analyze")
    p.add_argument("--black-detect", action="store_true",
                   help="Also detect black frames")

    p = sub.add_parser("pipeline", help="Run full generation pipeline")
    p.add_argument("brief", help="Brief JSON file")
    p.add_argument("--output-dir", "-o", default="./outputs")

    args = parser.parse_args()
    commands = {
        "review": cmd_review,
        "stitch": cmd_stitch,
        "subtitle": cmd_subtitle,
        "analyze": cmd_analyze,
        "pipeline": cmd_pipeline,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
