"""Check the portable guidance bundle and prevent accidental policy drift."""
from pathlib import Path
import json
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def main():
    """Validate shared policy routing and the intended read-only Claude reviewer."""
    settings = json.loads((ROOT / "settings.json").read_text())
    assert "hooks" not in settings
    assert settings["env"]["SLASH_COMMAND_TOOL_CHAR_BUDGET"] == "8000"
    assert settings["enabledPlugins"]["claude-engineering-foundation@claude-engineering-foundation"] is False
    assert not any((ROOT / name).exists() for name in ("rules", "commands", "hooks"))
    guides = sorted((ROOT / "guidance").glob("*.md"))
    assert len(guides) == 8
    for guide in guides:
        expected = (ROOT / "codex/guidance" / guide.name).read_text().replace("~/.codex", "~/.claude").replace("global `AGENTS.md`", "global `CLAUDE.md`")
        assert guide.read_text() == expected, guide.name
    claude = (ROOT / "CLAUDE.md").read_text()
    assert claude == (ROOT / "codex/AGENTS.md").read_text().replace("~/.codex", "~/.claude")
    for target in re.findall(r"\]\(~/.claude/([^)]*)\)", claude):
        assert (ROOT / target).is_file(), target
    for guide in guides:
        for target in re.findall(r"\]\(([^):]+\.md)\)", guide.read_text()):
            assert (guide.parent / target).is_file(), (guide.name, target)
    reviewer = (ROOT / "agents/reviewer.md").read_text()
    assert "tools: Read, Grep, Glob\n" in reviewer
    assert "disallowedTools: Write, Edit, NotebookEdit, Bash, Agent\n" in reviewer
    assert "model: inherit\n" in reviewer
    assert sorted(p.name for p in (ROOT / "agents").glob("*.md")) == ["reviewer.md"]
    assert sorted(p.name for p in (ROOT / "skills").iterdir()) == ["vault-knowledge"]
    codex_reviewer = tomllib.loads((ROOT / "codex/agents/reviewer.toml").read_text())
    assert codex_reviewer["sandbox_mode"] == "read-only"
    assert (ROOT / "scripts/read-guidance.py").read_bytes() == (ROOT / "codex/scripts/read-guidance.py").read_bytes()
    context = tomllib.loads((ROOT / "codex/context-settings.toml").read_text())
    assert context["skills"]["max_context_tokens"] == 2000
    assert len(context["skills"]["config"]) == 4
    assert all(entry["enabled"] is False for entry in context["skills"]["config"])
    assert (ROOT / "archive/2026-09-09/CLAUDE.md.disabled").is_file()
    print("Configuration checks passed: routing, eight shared guides, JSON/TOML, reviewer restrictions, and retired active surfaces.")


if __name__ == "__main__":
    main()
