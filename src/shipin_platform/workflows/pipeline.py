"""Pipeline orchestrator — multi-stage video generation workflow."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from shipin_platform.config import PlatformConfig, load_config
from shipin_platform.review.engine import ReviewEngine, ReviewReport, Decision


@dataclass
class PipelineState:
    """Mutable state carried through all pipeline stages."""
    project_id: str
    brief: dict = field(default_factory=dict)
    script: dict = field(default_factory=dict)
    storyboard: dict = field(default_factory=dict)
    character_profiles: dict = field(default_factory=dict)
    scene_profiles: dict = field(default_factory=dict)
    style_anchor: dict = field(default_factory=dict)
    image_prompts: dict = field(default_factory=dict)
    image_results: dict = field(default_factory=dict)
    video_prompts: dict = field(default_factory=dict)
    video_results: dict = field(default_factory=dict)
    final_output: Optional[Path] = None
    review_log: list[dict] = field(default_factory=list)
    total_cost_usd: float = 0.0
    current_stage: str = "brief"
    revision_history: dict = field(default_factory=dict)
    stage_outputs: dict = field(default_factory=dict)


class Pipeline:
    """Orchestrates the full video generation pipeline with multi-round iteration."""

    STAGES = ["brief", "script", "storyboard", "image_prompt", "image_gen",
              "video_prompt", "video_gen", "post_production"]

    def __init__(self, config: Optional[PlatformConfig] = None):
        self.config = config or load_config()
        self.review = ReviewEngine({
            "max_rounds": {s: self.config.review.max_rounds.get(s, 3)
                          for s in self.STAGES}
        })
        self.state = PipelineState(project_id="")

    def run_stage(self, stage: str, data: dict) -> ReviewReport:
        """Run one stage with review loop until PASS or MAX_ROUNDS.

        Between rounds, mechanical fixes (style anchor, subjective words,
        camera terms, I2V appearance cleanup) are applied to a copy of the
        data; findings without a mechanical fix are recorded in
        state.revision_history for the caller/LLM to regenerate.
        """
        max_r = self.config.review.max_rounds.get(stage, 3)
        prev_report: Optional[ReviewReport] = None
        fix_applied = False
        report: Optional[ReviewReport] = None
        self.state.stage_outputs[stage] = data

        for round_num in range(1, max_r + 1):
            report = self.review.run_review(
                stage, data, round_num=round_num,
                previous_report=prev_report,
                fix_applied=fix_applied,
            )
            self.state.review_log.append(report.to_dict())

            if report.decision in (Decision.PASS, Decision.PASS_WITH_WARNINGS,
                                   Decision.STALL, Decision.STOP):
                return report

            # REVISE → apply mechanical fixes and loop
            fix_result = self.review.revision.fix(stage, data, report)
            data = fix_result["data"]
            self.state.stage_outputs[stage] = data
            fix_applied = bool(fix_result["applied"])
            self.state.revision_history[f"{stage}_r{round_num}"] = {
                "applied": fix_result["applied"],
                "manual": fix_result["manual"],
                "notes": fix_result["notes"],
            }
            prev_report = report

        return report  # type: ignore[return-value]  # reached max rounds

    def run_full_pipeline(self, brief: dict) -> dict:
        """Run all stages with iteration."""
        self.state.brief = brief
        results = {"stages": {}, "final": None, "cost_usd": 0.0}

        for stage in self.STAGES:
            self.state.current_stage = stage
            # Check if stage has input data
            stage_data = self._get_stage_input(stage)
            if stage_data is None:
                continue

            report = self.run_stage(stage, stage_data)
            results["stages"][stage] = {
                "decision": report.decision.value,
                "rounds": report.round,
                "critical": report.critical_count,
                "suggestion": report.suggestion_count,
            }

            if report.decision.value in ("pass", "pass_with_warnings"):
                self._commit_stage_result(stage, report)
            else:
                results["stopped_at"] = stage
                break

        results["cost_usd"] = self.state.total_cost_usd
        return results

    def _get_stage_input(self, stage: str):
        """Get input data for a stage from state."""
        stage_inputs = {
            "brief": self.state.brief,
            "script": self.state.script,
            "storyboard": self.state.storyboard,
            "image_prompt": self.state.image_prompts,
            "image_gen": self.state.image_results,
            "video_prompt": self.state.video_prompts,
            "video_gen": self.state.video_results,
            "post_production": {},
        }
        return stage_inputs.get(stage)

    def _commit_stage_result(self, stage: str, report: ReviewReport):
        """Commit stage result to state."""
        if stage == "brief":
            self.state.brief = report.metadata.get("brief", self.state.brief)
        # Additional stage-specific commit logic here


# Convenience function
def create_pipeline(config: Optional[PlatformConfig] = None) -> Pipeline:
    """Factory function to create a configured pipeline."""
    return Pipeline(config)
