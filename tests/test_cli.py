"""CLI contract tests — the `shipin` entry point must be agent-usable."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SRC / "main.py"), *args],
        capture_output=True, text=True, timeout=120,
    )


class TestReviewCLI:
    def test_review_short_flag(self):
        brief = {"title": "x", "tone": "燃", "duration_sec": 60}
        tmp = SRC.parent / "tests" / "_brief_tmp.json"
        tmp.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        out = run_cli("review", str(tmp), "--stage", "brief", "--rounds", "2")
        tmp.unlink(missing_ok=True)
        assert out.returncode in (0, 1)
        body = json.loads(out.stdout)
        assert body["stage"] == "brief"
        assert 1 <= body["rounds_run"] <= 2
        assert body["decision"] in ("pass", "pass_with_warnings", "revise",
                                    "stall", "stop")
        assert "data" in body

    def test_review_rounds_capped(self):
        tmp = SRC.parent / "tests" / "_brief_tmp2.json"
        tmp.write_text(json.dumps({"tone": "燃"}, ensure_ascii=False),
                       encoding="utf-8")
        out = run_cli("review", str(tmp), "--stage", "brief", "--rounds", "2")
        tmp.unlink(missing_ok=True)
        body = json.loads(out.stdout)
        assert body["rounds_run"] <= 2


class TestStitchCliContract:
    def test_invalid_transition_rejected_by_parser(self):
        out = run_cli("stitch", "--clips", "a.mp4", "-o", "x.mp4",
                      "--transition", "dissolve")
        # argparse itself rejects unknown choice
        assert out.returncode != 0
        assert "invalid choice" in out.stderr or "usage" in out.stderr