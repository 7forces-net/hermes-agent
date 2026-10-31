"""Regression tests: a gateway must never inherit the delegation fence.

A ``delegate_task`` child that restarts the gateway copies ``os.environ``
verbatim into the respawn, so the ``HERMES_DELEGATED_CHILD_CONTEXT`` fence
marker rode along into the new gateway. The gateway then treated its own
board DB as a descendant's read-only target: every ``write_txn`` was refused.

Because the dispatch tick runs the reclaim phase FIRST, the reclaim raised,
the tick aborted before the spawn phase, and NO ready card was dispatched on
gamedev, default or techdad — with only one log line per board per minute to
show for it.

The gateway is the top-level control plane: it owns the boards and must be
able to mutate them. Every gateway spawn path now strips the fence (and the
worker board pins, which make every board slug resolve to one DB).
"""

from __future__ import annotations

import os

import pytest

from agent import delegation_context as dc


FENCE = dc.DELEGATED_CHILD_ENV_MARKER


def test_strip_delegation_fence_removes_the_marker():
    env = {FENCE: r"C:\Users\admin\AppData\Local\hermes", "PATH": "/bin"}
    cleaned = dc.strip_delegation_fence(env)
    assert FENCE not in cleaned
    assert cleaned["PATH"] == "/bin"


def test_strip_delegation_fence_removes_the_legacy_flag_value():
    """The legacy "1" value fences EVERY board — it must be removed too."""
    env = {FENCE: "1"}
    assert dc.strip_delegation_fence(env) == {}


def test_strip_delegation_fence_does_not_mutate_the_input():
    env = {FENCE: "1"}
    dc.strip_delegation_fence(env)
    assert FENCE in env


def test_strip_delegation_fence_is_the_opposite_of_scrub_kanban_env():
    """scrub_kanban_env PRESERVES an inherited fence (right for descendants);
    strip_delegation_fence REMOVES it (right for the control plane)."""
    env = {FENCE: r"C:\some\root"}
    assert dc.scrub_kanban_env(env)[FENCE] == r"C:\some\root"
    assert FENCE not in dc.strip_delegation_fence(env)


def test_control_plane_env_drops_the_fence_and_the_board_pins():
    """A control plane dispatches EVERY board.

    ``HERMES_KANBAN_DB`` is consulted BEFORE the ``board=`` argument in
    ``_board_path``, so an inherited pin makes every board slug resolve to
    that one DB — the gateway would dispatch the wrong board's cards.
    """
    env = {
        FENCE: r"C:\Users\admin\AppData\Local\hermes",
        "HERMES_KANBAN_DB": r"C:\Users\admin\AppData\Local\hermes\kanban\boards\gamedev\kanban.db",
        "HERMES_KANBAN_BOARD": "gamedev",
        "HERMES_KANBAN_WORKSPACES_ROOT": r"C:\...\workspaces",
        "HERMES_KANBAN_WORKSPACE": r"C:\...\workspaces\t_1",
        "PATH": "/bin",
    }
    cleaned = dc.control_plane_env(env)
    for key in (FENCE, "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_WORKSPACE"):
        assert key not in cleaned, f"{key} must not reach a control plane"
    assert cleaned["PATH"] == "/bin"


def test_control_plane_env_keeps_unrelated_vars():
    env = {"PATH": "/bin", "HOME": "/root", "HERMES_HOME": "/h"}
    cleaned = dc.control_plane_env(env)
    assert cleaned == {"PATH": "/bin", "HOME": "/root", "HERMES_HOME": "/h"}


def test_a_fenced_gateway_cannot_mutate_its_own_board(monkeypatch, tmp_path):
    """The fence really does refuse board writes — that is the outage.

    Documents WHY the gateway must not inherit it.
    """
    monkeypatch.setenv(FENCE, str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    assert dc.kanban_path_is_fenced(tmp_path / "kanban.db") is True


def test_an_unfenced_gateway_can_mutate_its_own_board(monkeypatch, tmp_path):
    """The inverse: with the marker gone the same path is writable."""
    monkeypatch.delenv(FENCE, raising=False)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "kanban.db"))
    assert dc.kanban_path_is_fenced(tmp_path / "kanban.db") is False


def test_gateway_restart_watcher_env_has_no_fence(monkeypatch):
    """The in-gateway /restart path must not hand the fence to the new gateway."""
    from gateway.run_shutdown import GatewayShutdownMixin

    monkeypatch.setenv(FENCE, r"C:\Users\admin\AppData\Local\hermes")
    monkeypatch.setenv("HERMES_KANBAN_DB", r"C:\...\gamedev\kanban.db")
    monkeypatch.setenv("_HERMES_GATEWAY", "1")

    env = GatewayShutdownMixin._restart_watcher_env()
    assert FENCE not in env
    assert "_HERMES_GATEWAY" not in env
    assert "HERMES_KANBAN_DB" not in env


def test_gateway_respawn_watcher_source_strips_the_fence():
    """The inlined respawn-watcher source must build its env without the fence.

    The watcher source is a template string; assert on the literal so a
    refactor that reintroduces ``{**os.environ}`` is caught here.
    """
    from hermes_cli import gateway as gw

    source = inspect_getsource_of_respawn_watcher(gw)
    assert "HERMES_DELEGATED_CHILD_CONTEXT" in source
    assert "{**os.environ" not in source, (
        "the respawn env must not copy os.environ wholesale — that is how the "
        "delegation fence reached the gateway"
    )


def inspect_getsource_of_respawn_watcher(gw) -> str:
    """The respawn watcher's source template, as text."""
    import inspect

    return inspect.getsource(gw._spawn_gateway_restart_watcher)


@pytest.mark.skipif(os.name != "nt", reason="Windows detached-spawn path")
def test_windows_spawn_detached_drops_the_fence(monkeypatch):
    """The Windows direct-spawn path must not hand the fence to the gateway."""
    from hermes_cli import gateway_windows as gw

    captured: dict = {}

    class _Proc:
        pid = 4242

    def fake_popen(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(gw.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(gw, "_build_gateway_argv", lambda home=None: (["py", "-m", "x"], ".", {}))
    monkeypatch.setenv(FENCE, r"C:\Users\admin\AppData\Local\hermes")

    gw._spawn_detached()
    assert captured.get("env") is not None
    assert FENCE not in captured["env"]
