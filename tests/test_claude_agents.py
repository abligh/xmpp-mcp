"""Unit tests for contrib/claude-agents/claude-agents — the directory-based supervisor.

No tmux and no Claude Code: these cover what the supervisor *decides* — which
directories are agents, which conversation each resumes, how a fork is laid
out, and the command line it would run.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "contrib" / "claude-agents" / "claude-agents"
_loader = importlib.machinery.SourceFileLoader("claude_agents", str(SCRIPT))
_spec = importlib.util.spec_from_loader("claude_agents", _loader)
ca = importlib.util.module_from_spec(_spec)
sys.modules["claude_agents"] = ca  # dataclasses look their module up
_loader.exec_module(ca)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CLAUDE_AGENTS_STATE", str(tmp_path / "state"))
    (tmp_path / "agents").mkdir()
    return tmp_path


def _conversation(real: Path, session: str, *, age: float = 0.0) -> Path:
    d = ca.project_dir(real)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{session}.jsonl"
    f.write_text('{"type":"user"}\n')
    t = time.time() - age
    os.utime(f, (t, t))
    return f


def test_slug_matches_claude_codes_project_folders() -> None:
    # A real pair: this is how Claude Code filed a worktree's conversations.
    path = "/home/claude/go/src/github.com/abligh/xmpp-mcp/.claude/worktrees/bridge-cse_01Fd5gTSCifqhuKFVjT58xrP"
    assert ca.slug(path) == (
        "-home-claude-go-src-github-com-abligh-xmpp-mcp--claude-worktrees-bridge-cse-01Fd5gTSCifqhuKFVjT58xrP"
    )


def test_config_splits_own_settings_from_the_agents_environment(tmp_path: Path) -> None:
    conf = tmp_path / "config.env"
    conf.write_text(
        "# comment\n"
        "AGENTS_ROOT=~/crew\n"
        "AGENTS_CLAUDE_ARGS=--model sonnet\n"
        "XMPP_JID={session}.{host}@agents.example.com\n"
        "XMPP_CHANNEL_ALLOW='*@agents.example.com,*@example.com'\n"
    )
    cfg = ca.load_config(conf)
    assert cfg.root == Path("~/crew").expanduser()
    assert cfg.extra_args == ["--model", "sonnet"]
    assert cfg.plugin == "xmpp@xmpp-mcp"
    assert cfg.session_env == {
        "XMPP_JID": "{session}.{host}@agents.example.com",
        "XMPP_CHANNEL_ALLOW": "*@agents.example.com,*@example.com",
    }


def test_agents_are_directories_and_symlinks_to_them(home: Path) -> None:
    root = home / "agents"
    (root / "reviewer").mkdir()
    (home / "elsewhere").mkdir()
    (root / "builder").symlink_to(home / "elsewhere")
    (root / ".hidden").mkdir()
    (root / "notes.txt").write_text("not an agent")
    (root / "bad name").mkdir()
    (root / "dangling").symlink_to(home / "missing")
    agents = {a.name: a for a in ca.list_agents(root)}
    assert set(agents) == {"builder", "reviewer"}
    assert agents["builder"].real == (home / "elsewhere").resolve()


def test_resumes_its_own_conversation_else_the_newest(home: Path) -> None:
    real = home / "agents" / "reviewer"
    real.mkdir()
    agent = ca.Agent("reviewer", real, real)
    assert ca.session_to_resume(agent, {}) is None  # nothing yet: a new conversation
    _conversation(real, "older", age=100)
    _conversation(real, "newer")
    assert ca.session_to_resume(agent, {}) == "newer"
    # The one we last ran wins, even if something else ran there since...
    assert ca.session_to_resume(agent, {"session": "older"}) == "older"
    # ...unless its transcript has gone.
    assert ca.session_to_resume(agent, {"session": "deleted"}) == "newer"


def test_command_line(home: Path) -> None:
    real = home / "agents" / "reviewer"
    real.mkdir()
    agent = ca.Agent("reviewer", real, real)
    cfg = ca.Config(root=home / "agents", extra_args=["--model", "sonnet"])
    fresh = ca.claude_args(agent, cfg, {})
    assert fresh[:7] == ["claude", "--remote-control", "reviewer", "--name", "reviewer",
                         "--channels", "plugin:xmpp@xmpp-mcp"]
    assert json.loads(fresh[fresh.index("--settings") + 1]) == {
        "enabledPlugins": {"xmpp@xmpp-mcp": True}}
    assert "--resume" not in fresh and fresh[-2:] == ["--model", "sonnet"]
    _conversation(real, "s1")
    resumed = ca.claude_args(agent, cfg, {"session": "s1"})
    assert resumed[resumed.index("--resume") + 1] == "s1" and "--fork-session" not in resumed
    forked = ca.claude_args(agent, cfg, {"fork_from": "p1"})
    assert forked[forked.index("--resume") + 1] == "p1" and "--fork-session" in forked


def test_fork_files_a_copy_of_the_parents_conversation_under_the_child(
    home: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ca, "pane", lambda name: None)  # the parent is not running
    root = home / "agents"
    (root / "parent").mkdir()
    parent = ca.Agent("parent", root / "parent", (root / "parent").resolve())
    _conversation(parent.real, "p1")
    extra = ca.project_dir(parent.real) / "p1" / "subagents"
    extra.mkdir(parents=True)
    (extra / "a.jsonl").write_text("{}\n")
    ca.save_state("parent", {"session": "p1"})

    cfg = ca.Config(root=root)
    child = ca.prepare_fork(cfg, parent, "child", target=None)
    assert child.real == (root / "child").resolve() and child.real.is_dir()
    copied = ca.project_dir(child.real)
    assert (copied / "p1.jsonl").read_text() == '{"type":"user"}\n'
    assert (copied / "p1" / "subagents" / "a.jsonl").exists()
    assert ca.load_state("child") == {"fork_from": "p1", "forked_from_agent": "parent"}
    # The parent's own conversation is untouched.
    assert (ca.project_dir(parent.real) / "p1.jsonl").exists()


def test_fork_refuses_a_parent_with_no_conversation(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ca, "pane", lambda name: None)
    root = home / "agents"
    (root / "parent").mkdir()
    parent = ca.Agent("parent", root / "parent", (root / "parent").resolve())
    with pytest.raises(SystemExit, match="no conversation to fork"):
        ca.prepare_fork(ca.Config(root=root), parent, "child", target=None)
    assert not (root / "child").exists()
