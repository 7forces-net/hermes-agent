"""The per-card reasoning-effort override: the CLI verbs that make it reachable.

The board already had every piece of the plumbing EXCEPT the CLI verb:
``tasks.reasoning_effort`` (column), ``normalize_reasoning_effort()``,
``set_reasoning_effort()`` (which had ZERO callers) and the dispatcher's
``--reasoning`` argument. ``hermes kanban create`` had no ``--reasoning`` flag and
``set-model`` handled only model/provider, so an operator could neither request
``xhigh`` for one hard card nor pin ``high`` for one card while the profile default
differed.

These tests pin the newly reachable surface:

* ``hermes kanban create --reasoning <level>`` persists the level.
* ``hermes kanban set-effort <task> <level>`` sets it on an EXISTING card and
  ``set-effort <task> clear`` returns it to the profile default.
* A bogus level is REJECTED with the accepted list (never silently written).
* ``none`` is a VALUE (thinking off), not a clear.
* The dispatched worker receives ``--reasoning <level>`` in its argv.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_parser import build_parser


ACCEPTED = "none, minimal, low, medium, high, xhigh, max, ultra"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _parse(argv):
    """Parse a ``hermes kanban …`` argv through the real parser tree."""
    wrap = argparse.ArgumentParser(prog="hermes kanban", add_help=False)
    wrap.exit_on_error = False
    parser = build_parser(wrap.add_subparsers(dest="_top"))
    return parser.parse_args(argv)


def _run(argv):
    return kc.kanban_command(_parse(argv))


def _only_task():
    with kbc.connect_closing() as conn:
        tasks = kb.list_tasks(conn)
        assert len(tasks) == 1, f"expected exactly one card, got {len(tasks)}"
        return kb.get_task(conn, tasks[0].id)


# ---------------------------------------------------------------------------
# create --reasoning
# ---------------------------------------------------------------------------


class TestCreateWithReasoning:
    def test_create_persists_the_level(self, kanban_home):
        assert _run(["create", "a card", "--reasoning", "high"]) == 0
        assert _only_task().reasoning_effort == "high"

    def test_create_accepts_the_whole_ladder(self, kanban_home):
        for level in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            assert _run(["create", f"card {level}", "--reasoning", level]) == 0
        with kbc.connect_closing() as conn:
            levels = [t.reasoning_effort for t in kb.list_tasks(conn)]
        assert sorted(levels) == sorted(
            ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]
        )

    def test_create_normalizes_case_and_whitespace(self, kanban_home):
        assert _run(["create", "a card", "--reasoning", "  HIGH "]) == 0
        assert _only_task().reasoning_effort == "high"

    def test_create_without_the_flag_leaves_the_profile_default(self, kanban_home):
        assert _run(["create", "a card"]) == 0
        assert _only_task().reasoning_effort is None

    def test_create_rejects_a_bogus_level_with_the_accepted_list(self, kanban_home, capsys):
        rc = _run(["create", "a card", "--reasoning", "bogus"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "bogus" in err
        for level in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            assert level in err, f"the accepted list must name {level}"

    def test_a_rejected_level_writes_nothing(self, kanban_home):
        _run(["create", "a card", "--reasoning", "bogus"])
        with kbc.connect_closing() as conn:
            assert kb.list_tasks(conn) == [], "a rejected level must not create a card"

    def test_create_works_without_a_model_override(self, kanban_home):
        """Depth and model are independent knobs."""
        assert _run(["create", "a card", "--reasoning", "low"]) == 0
        task = _only_task()
        assert task.model_override is None
        assert task.reasoning_effort == "low"


# ---------------------------------------------------------------------------
# set-effort verb
# ---------------------------------------------------------------------------


class TestSetEffortVerb:
    def test_set_effort_sets_the_level_on_an_existing_card(self, kanban_home):
        _run(["create", "a card"])
        task_id = _only_task().id
        assert _run(["set-effort", task_id, "xhigh"]) == 0
        with kbc.connect_closing() as conn:
            assert kb.get_task(conn, task_id).reasoning_effort == "xhigh"

    def test_set_effort_clear_returns_to_the_profile_default(self, kanban_home):
        _run(["create", "a card", "--reasoning", "high"])
        task_id = _only_task().id
        assert _run(["set-effort", task_id, "clear"]) == 0
        with kbc.connect_closing() as conn:
            assert kb.get_task(conn, task_id).reasoning_effort is None

    def test_set_effort_without_a_level_clears(self, kanban_home):
        _run(["create", "a card", "--reasoning", "high"])
        task_id = _only_task().id
        assert _run(["set-effort", task_id]) == 0
        with kbc.connect_closing() as conn:
            assert kb.get_task(conn, task_id).reasoning_effort is None

    def test_none_is_a_value_not_a_clear(self, kanban_home):
        """``none`` pins thinking OFF — it must not be mistaken for "clear"."""
        _run(["create", "a card", "--reasoning", "high"])
        task_id = _only_task().id
        assert _run(["set-effort", task_id, "none"]) == 0
        with kbc.connect_closing() as conn:
            assert kb.get_task(conn, task_id).reasoning_effort == "none"

    def test_set_effort_rejects_a_bogus_level(self, kanban_home, capsys):
        _run(["create", "a card", "--reasoning", "high"])
        task_id = _only_task().id
        rc = _run(["set-effort", task_id, "bogus"])
        assert rc == 2
        assert "bogus" in capsys.readouterr().err
        with kbc.connect_closing() as conn:
            assert kb.get_task(conn, task_id).reasoning_effort == "high", (
                "a rejected level must leave the previous value untouched"
            )

    def test_set_effort_on_an_unknown_task_is_an_error(self, kanban_home, capsys):
        assert _run(["set-effort", "t_doesnotexist", "high"]) != 0
        assert "no such task" in capsys.readouterr().err

    def test_set_effort_does_not_touch_the_model_override(self, kanban_home):
        """Depth and model are independent knobs (one must not reset the other)."""
        _run(["create", "a card", "--model", "glm-5", "--provider", "openrouter"])
        task_id = _only_task().id
        _run(["set-effort", task_id, "ultra"])
        with kbc.connect_closing() as conn:
            task = kb.get_task(conn, task_id)
        assert task.model_override == "glm-5"
        assert task.provider_override == "openrouter"
        assert task.reasoning_effort == "ultra"

    def test_the_override_is_recorded_as_a_task_event(self, kanban_home):
        _run(["create", "a card"])
        task_id = _only_task().id
        _run(["set-effort", task_id, "max"])
        with kbc.connect_closing() as conn:
            kinds = [e.kind for e in kb.list_events(conn, task_id)]
        assert "reasoning_effort_set" in kinds

    def test_clearing_is_also_recorded(self, kanban_home):
        _run(["create", "a card", "--reasoning", "max"])
        task_id = _only_task().id
        _run(["set-effort", task_id, "clear"])
        with kbc.connect_closing() as conn:
            events = [e for e in kb.list_events(conn, task_id) if e.kind == "reasoning_effort_set"]
        assert events, "the clear must be recorded too"
        assert events[-1].payload["reasoning_effort"] is None


# ---------------------------------------------------------------------------
# The dispatched worker receives the level
# ---------------------------------------------------------------------------


def _spawn_and_capture(monkeypatch, tmp_path, task):
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    captured = {}

    class FakeProc:
        pid = 4245

    def fake_popen(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    kbd._default_spawn(task, str(workspace))
    return captured["cmd"]


class TestWorkerArgv:
    def test_the_worker_is_spawned_with_the_level(self, kanban_home, monkeypatch, tmp_path):
        _run(["create", "a card", "--assignee", "worker", "--reasoning", "high"])
        task = _only_task()
        cmd = _spawn_and_capture(monkeypatch, tmp_path, task)
        i = cmd.index("--reasoning")
        assert cmd[i + 1] == "high"

    def test_no_level_means_no_flag(self, kanban_home, monkeypatch, tmp_path):
        _run(["create", "a card", "--assignee", "worker"])
        task = _only_task()
        cmd = _spawn_and_capture(monkeypatch, tmp_path, task)
        assert "--reasoning" not in cmd

    def test_the_level_travels_with_a_pinned_model(self, kanban_home, monkeypatch, tmp_path):
        """Depth and model are independent: both flags ride on the same argv."""
        _run(["create", "a card", "--assignee", "worker",
              "--model", "glm-5", "--provider", "openrouter", "--reasoning", "xhigh"])
        task = _only_task()
        cmd = _spawn_and_capture(monkeypatch, tmp_path, task)
        assert cmd[cmd.index("-m") + 1] == "glm-5"
        assert cmd[cmd.index("--provider") + 1] == "openrouter"
        assert cmd[cmd.index("--reasoning") + 1] == "xhigh"

    def test_the_worker_cli_accepts_the_flag(self):
        """A spawn argument no parser accepts fails every dispatch."""
        from hermes_cli._parser import build_top_level_parser

        parser = build_top_level_parser()[0]
        args = parser.parse_args(["--cli", "chat", "-q", "hi", "--reasoning", "xhigh"])
        assert args.reasoning == "xhigh"

    def test_the_override_survives_the_fallback_leg(self):
        """The per-card level must outrank the profile default on EVERY leg.

        The fallback activation re-resolves ``reasoning_config`` from config.yaml.
        An explicit per-run override (what ``--reasoning`` carries) has to win there
        too, or a per-card ``xhigh`` silently reverts on exactly the leg the card
        was pinning.
        """
        from agent.chat_completion_helpers import _reresolve_fallback_reasoning_config

        agent = type("A", (), {})()
        agent.model = "fallback-model"
        agent.reasoning_config = {"enabled": True, "effort": "high"}
        agent._reasoning_effort_override = {"enabled": True, "effort": "xhigh"}

        _reresolve_fallback_reasoning_config(agent)
        assert agent.reasoning_config == {"enabled": True, "effort": "xhigh"}

    def test_without_an_override_the_fallback_still_resolves_from_config(self):
        """The pre-existing per-model/config resolution is unchanged."""
        from agent.chat_completion_helpers import _reresolve_fallback_reasoning_config

        agent = type("A", (), {})()
        agent.model = "fallback-model"
        agent.reasoning_config = {"enabled": True, "effort": "high"}
        agent._reasoning_effort_override = None

        _reresolve_fallback_reasoning_config(agent)
        assert agent.reasoning_config != {"enabled": True, "effort": "xhigh"}


# ---------------------------------------------------------------------------
# The vocabulary is not widened
# ---------------------------------------------------------------------------


class TestVocabulary:
    def test_the_ladder_is_exactly_the_shipped_one(self):
        from hermes_constants import VALID_REASONING_EFFORTS

        assert VALID_REASONING_EFFORTS == (
            "minimal", "low", "medium", "high", "xhigh", "max", "ultra",
        )
        # ``none`` is the thinking-off VALUE, handled separately by the normalizer.
        assert kb.normalize_reasoning_effort("none") == "none"

    def test_xhigh_stays_available(self):
        """``xhigh`` is a legitimate per-card request (it is what Item 1 delivers)."""
        assert kb.normalize_reasoning_effort("xhigh") == "xhigh"

    @pytest.mark.parametrize("bogus", ["extreme", "highest", "maxx", "11", "off", "disable"])
    def test_bogus_levels_are_rejected_by_the_normalizer(self, bogus):
        with pytest.raises(ValueError):
            kb.normalize_reasoning_effort(bogus)
