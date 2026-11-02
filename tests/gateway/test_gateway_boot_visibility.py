"""The gateway must be VISIBLE while it boots, not only once it is serving.

Regression tests for the restart-storm class of failure: the runtime lock and
``gateway.pid`` used to be claimed at the very END of startup, so a gateway that
was minutes into its boot had no identity on disk. Every liveness probe read
"no gateway here", concluded it had died, and started another instance — and
because none of the instances had claimed the slot yet, they stacked. One slow
boot on a memory-starved host became a dozen concurrent gateways.

These tests pin the two halves of the fix:

1. The boot slot (runtime lock + PID file) is claimed BEFORE the heavy startup
   work, so a second instance refuses immediately instead of racing through the
   same minutes-long startup.
2. A boot marker records "this gateway is booting" for the window before the
   authoritative claim, so a supervisor can tell "still booting" from "dead".
"""

import json
import os
import time
from pathlib import Path

import pytest

from gateway import status


@pytest.fixture(autouse=True)
def _reset_status_singletons():
    """The runtime-lock handle and PID cache are module globals; keep tests isolated."""
    status.release_gateway_runtime_lock()
    yield
    status.release_gateway_runtime_lock()


class TestBootSlotClaim:
    """The slot must be claimable BEFORE the heavy startup work."""

    def test_claim_boot_slot_writes_pid_and_lock(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        assert status.claim_gateway_boot_slot() is True

        # The PID file exists immediately — this is the whole point: a booting
        # gateway is visible to every probe that reads gateway.pid.
        payload = json.loads((tmp_path / "gateway.pid").read_text())
        assert payload["pid"] == os.getpid()
        assert payload["kind"] == "hermes-gateway"
        # And the runtime lock is held, so a second instance is excluded.
        assert status.owns_gateway_runtime_lock() is True
        assert status.is_gateway_runtime_lock_active() is True

    def test_second_claim_in_the_same_process_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        assert status.claim_gateway_boot_slot() is True
        assert status.claim_gateway_boot_slot() is True

    def test_claim_removes_a_stale_pid_file_from_a_crashed_boot(self, tmp_path, monkeypatch):
        """A crashed gateway leaves gateway.pid behind; the next boot must not be blocked by it.

        The normal stale-cleanup path only unlinks a PID file when the runtime
        lock is INACTIVE — but the claim holds the lock, so a leftover file from
        a crashed gateway would make the O_EXCL write fail and refuse a boot
        that should succeed.
        """
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # A PID file naming a dead process (a PID that certainly does not exist).
        dead_pid = 0x7FFFFFFF
        (tmp_path / "gateway.pid").write_text(
            json.dumps({
                "pid": dead_pid, "kind": "hermes-gateway",
                "argv": ["hermes", "gateway", "run"], "start_time": 1.0,
                "hermes_home": str(tmp_path),
            }),
            encoding="utf-8",
        )

        assert status.claim_gateway_boot_slot() is True

        payload = json.loads((tmp_path / "gateway.pid").read_text())
        assert payload["pid"] == os.getpid()

    def test_claim_refuses_when_another_live_gateway_owns_the_home(self, tmp_path, monkeypatch):
        """A PID file naming a LIVE gateway must never be clobbered by a boot."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        # This process is alive, so a record naming it counts as "a live gateway".
        (tmp_path / "gateway.pid").write_text(
            json.dumps({
                "pid": os.getpid(), "kind": "hermes-gateway",
                "argv": ["hermes", "gateway", "run"], "start_time": None,
                "hermes_home": str(tmp_path),
            }),
            encoding="utf-8",
        )

        # The lock is not held, but the PID file names a live gateway -> refuse.
        assert status.claim_gateway_boot_slot() is False
        # The live gateway's record survived untouched.
        payload = json.loads((tmp_path / "gateway.pid").read_text())
        assert payload["pid"] == os.getpid()


class TestBootMarker:
    """The marker tells a supervisor that a boot is UNDERWAY, not that it is dead."""

    def test_write_boot_marker_records_this_process(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        status.write_boot_marker()

        record = status.read_boot_marker()
        assert record is not None
        assert record["pid"] == os.getpid()
        assert record["phase"] == "booting"
        assert record["boot_started_at"]

    def test_boot_in_progress_is_true_while_the_process_is_alive(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        status.write_boot_marker()

        assert status.gateway_boot_in_progress() is True

    def test_boot_in_progress_is_false_without_a_marker(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        assert status.gateway_boot_in_progress() is False

    def test_boot_in_progress_is_false_for_a_dead_boot(self, tmp_path, monkeypatch):
        """A marker left behind by a crashed boot must not read as a live boot."""
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

        assert status.gateway_boot_in_progress() is False

    def test_boot_in_progress_expires_past_its_window(self, tmp_path, monkeypatch):
        """Past the window a boot is presumed wedged, so a supervisor takes over."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        status.write_boot_marker()

        # A zero window means "already expired".
        assert status.gateway_boot_in_progress(max_age_s=0.0) is False
        assert status.gateway_boot_in_progress(max_age_s=3600.0) is True
        # A negative window disables the age limit: trust the marker while its
        # process is alive.
        assert status.gateway_boot_in_progress(max_age_s=-1.0) is True

    def test_clear_boot_marker_removes_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        status.write_boot_marker()
        assert status.read_boot_marker() is not None

        status.clear_boot_marker()
        assert status.read_boot_marker() is None

    def test_read_boot_marker_does_not_unlink(self, tmp_path, monkeypatch):
        """Reading the marker must be side-effect free — a watcher polls it."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        status.write_boot_marker()
        path = tmp_path / "gateway.booting.json"

        status.read_boot_marker()

        assert path.exists()

    def test_boot_marker_never_makes_a_process_the_gateway(self, tmp_path, monkeypatch):
        """The marker is a hint, not authority: only the O_EXCL claim is the gateway."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        status.write_boot_marker()

        # No gateway.pid was written, so there is no running gateway.
        assert not (tmp_path / "gateway.pid").exists()
        assert status.get_running_pid() is None
