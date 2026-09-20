"""开放层契约：Skill 元数据 / mcp 接入配置 / exec 记录自检。

验收：skill 能被任何 skill 加载器（ZCode 等）解析 frontmatter；
mcp/shipin.mcp.json 可被 JSON 客户端消费；文档与成品仓库一致。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_skill_frontmatter_and_sections():
    p = ROOT / "agents" / "shipin-platform" / "SKILL.md"
    t = p.read_text(encoding="utf-8")
    fm = re.match(r"\A---\n(.*?)\n---\n", t, re.S)
    assert fm, "SKILL.md 必须以 YAML frontmatter 开头"
    body = fm.group(1)
    assert re.search(r"^name:\s*shipin-platform\s*$", body, re.M)
    assert re.search(r"^description:\s*[>|]?-?", body, re.M)
    for sec in ("## 1.", "## 2.", "## 3.", "## 4.", "## 5.", "## 6."):
        assert sec in t, f"缺少章节 {sec}"
    # 铁律关键字（防止 Skill 被改丢红线）
    for kw in ("approved_by", "rewrite_stage", "keypool", "QC 拦截",
               "SHIPIN_KEY_SEAL"):
        assert kw in t, f"SKILL.md 缺少红线关键字 {kw}"


def test_mcp_config_json_parseable():
    cfg = json.loads((ROOT / "mcp" / "shipin.mcp.json")
                     .read_text(encoding="utf-8"))
    s = cfg["mcpServers"]["shipin"]
    assert s["command"] == "python"
    assert s["type"] == "stdio"
    assert "mcp_server.py" in s["args"][0]


def test_mcp_readme_lists_all_tools():
    readme = (ROOT / "mcp" / "README.md").read_text(encoding="utf-8")
    for tool in ("shipin_health", "shipin_list_projects", "shipin_project_status",
                 "shipin_list_events", "shipin_get_artifact", "shipin_preview_frames",
                 "shipin_pipeline_text", "shipin_project_confirm",
                 "shipin_rewrite_stage", "shipin_preflight", "shipin_pipeline_generate",
                 "shipin_pipeline_assemble", "shipin_report", "shipin_set_budget",
                 "shipin_ingest_reference", "shipin_integrity"):
        assert tool in readme, f"README 缺工具 {tool}"


def test_desktop_bat_present():
    bat = ROOT / "start-desktop.bat"
    assert bat.is_file()
    text = bat.read_text(encoding="utf-8", errors="replace")
    assert "desktop_app.py" in text