"""``hermes gateway watchdog`` must tell "still booting" from "dead".

The failure this pins: a supervisor that only asks "is ``gateway.pid``
present?" concludes "dead" for the whole of a multi-minute boot and starts
another instance every tick. Those instances stack, because none of them has
claimed the runtime lock yet — twelve concurrent gateways on a memory-starved
host is the measured outcome of exactly that loop.

The watchdog's answer must therefore be:
* a live gateway      -> healthy
* a gateway BOOTING   -> healthy (and nothing is started)
* neither             -> not healthy (and only then may a start be considered)
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gateway import status
from hermes_cli import gateway_watchdog as gw


@pytest.fixture(autouse=True)
def _reset_status_singletons():
    status.release_gateway_runtime_lock()
    yield
    status.release_gateway_runtime_lock()


class TestWatchdogProbe:
    def test_no_gateway_and_no_boot_is_not_healthy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        probe = gw._probe_status(tmp_path)

        assert probe.running_pid is None
        assert probe.boot_in_progress is False
        assert probe.healthy is False

    def test_a_boot_in_progress_is_healthy(self, tmp_path, monkeypatch):
        """THE regression: a booting gateway must NOT read as a dead gateway."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        status.write_boot_marker()

        probe = gw._probe_status(tmp_path)

        assert probe.boot_in_progress is True
        assert probe.healthy is True
        assert "boot in progress" in probe.reason

    def test_a_live_gateway_is_healthy(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        status.claim_gateway_boot_slot()

        probe = gw._probe_status(tmp_path)

        assert probe.running_pid == os.getpid()
        assert probe.healthy is True

    def test_a_dead_boot_marker_does_not_count(self, tmp_path, monkeypatch):
        """A marker from a crashed boot must not make the watchdog back off forever."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / "gateway.booting.json").write_text(
            json.dumps({
                "pid": 0x7FFFFFFF, "kind": "hermes-gateway",
                "argv": ["hermes", "gateway", "run"], "start_time": None,
                "hermes_home": str(tmp_path),
                "phase": "booting", "boot_started_at": "2026-01-01T00:00:00+00:00",
            }),
            encoding="utf-8",
        )

        probe = gw._probe_status(tmp_path)

        assert probe.boot_in_progress is False
        assert probe.healthy is False


class TestWatchdogEnsure:
    def test_ensure_does_not_start_when_a_boot_is_in_progress(self, tmp_path, monkeypatch):
        """The acceptance criterion: a booting gateway is never joined by a second one."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        status.write_boot_marker()

        probe = gw.ensure_gateway_running(tmp_path, start=True, dry_run=True)

        assert probe.healthy is True
        # Nothing was launched: the reason still reports the boot, not a start.
        assert "boot in progress" in probe.reason
        assert "started via" not in probe.reason

    def test_ensure_without_start_flag_never_launches(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        probe = gw.ensure_gateway_running(tmp_path, start=False)

        assert probe.healthy is False
        assert "--start not given" in probe.reason

    def test_ensure_reports_a_start_when_there_is_no_gateway(self, tmp_path, monkeypatch):
        """With no gateway and no boot, a start IS warranted — and is reported."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        probe = gw.ensure_gateway_running(tmp_path, start=True, dry_run=True)

        assert probe.healthy is False
        assert "dry run" in probe.reason


class TestWatchdogCli:
    """The command must work end-to-end through the real CLI."""

    def _run(self, home: Path, *args: str) -> tuple[int, str]:
        env = dict(os.environ)
        env["HERMES_HOME"] = str(home)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        proc = subprocess.run(
            [sys.executable, "-m", "hermes_cli.main", "gateway", "watchdog", *args],
            capture_output=True, text=True, timeout=180, env=env,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def test_cli_status_reports_a_booting_gateway_as_healthy(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()

        # A live process publishes the boot marker for this home.
        marker = home / "gateway.booting.json"
        marker.write_text(json.dumps({
            "pid": os.getpid(), "kind": "hermes-gateway",
            "argv": ["hermes", "gateway", "run"], "start_time": None,
            "hermes_home": str(home),
            "phase": "booting", "boot_started_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        }), encoding="utf-8")

        rc, out = self._run(home, "status", "--json")

        payload = json.loads(out[out.index("{"):out.rindex("}") + 1])
        assert payload["boot_in_progress"] is True
        assert payload["healthy"] is True
        # rc 0 == healthy, so a supervisor's "is it OK?" check passes.
        assert rc == 0

    def test_cli_status_reports_no_gateway_as_unhealthy(self, tmp_path):
        home = tmp_path / "home"
        home.mkdir()

        rc, out = self._run(home, "status", "--json")

        payload = json.loads(out[out.index("{"):out.rindex("}") + 1])
        assert payload["healthy"] is False
        assert rc == 1
