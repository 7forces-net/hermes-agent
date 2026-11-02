"""``hermes gateway watchdog`` — repo-side gateway supervision with a boot-aware liveness check.

WHY THIS EXISTS
---------------
A gateway's boot is long: the import graph, then a ``state.db`` migration on a
multi-GB database, then the platform connects. On a host that has been measured
at ~13 minutes for that boot, an ops-level watchdog that asks only "is
``gateway.pid`` present?" concludes "the gateway is dead" for the ENTIRE boot
and starts another instance every tick. Because the gateway claims its runtime
lock and PID file only near the END of startup, none of those instances can see
each other, and they stack — twelve concurrent gateways on a machine with no
free memory is the measured outcome of exactly this loop.

So the watchdog asks the right question:

    Is there a live gateway for this HERMES_HOME, OR is one booting?

"Booting" is answered by the boot marker (``gateway.booting.json``), which the
gateway publishes before its heavy startup work and removes once the
authoritative PID-file claim lands. A marker whose process is dead, or whose
start time no longer matches the record (PID reuse), does not count.

WHAT IT DOES
------------
``status``   report the gateway's liveness, and whether a boot is in progress.
``ensure``   start the gateway only when there is genuinely no live instance
              AND no boot in progress. This is the safe form of the old
              "PID file absent -> start" rule.

By default NOTHING is started: ``status`` is the default action and ``ensure``
requires an explicit flag. A supervisor that starts gateways on its own is
exactly the thing that caused the storm, so the dangerous action is opt-in.

The restart mechanism is the platform's own service manager (systemd, launchd,
the Windows Scheduled Task) — the same one ``hermes gateway start`` uses — so
the watchdog never invents its own launch path.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_BOOT_WINDOW_S = 30 * 60


@dataclass
class GatewayWatchdogStatus:
    """What the watchdog knows about this home's gateway."""

    home: str
    running_pid: Optional[int] = None
    pid_file_present: bool = False
    boot_in_progress: bool = False
    boot_pid: Optional[int] = None
    boot_started_at: Optional[str] = None
    boot_age_s: Optional[float] = None
    live_process_pids: list[int] = field(default_factory=list)
    reason: str = ""

    @property
    def healthy(self) -> bool:
        """True when a gateway is serving, or is credibly on its way to serving."""
        return self.running_pid is not None or self.boot_in_progress

    def to_dict(self) -> dict:
        return {
            "home": self.home,
            "running_pid": self.running_pid,
            "pid_file_present": self.pid_file_present,
            "boot_in_progress": self.boot_in_progress,
            "boot_pid": self.boot_pid,
            "boot_started_at": self.boot_started_at,
            "boot_age_s": self.boot_age_s,
            "live_process_pids": self.live_process_pids,
            "reason": self.reason,
            "healthy": self.healthy,
        }


def _hermes_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home())


def _probe_status(home: Path, boot_window_s: int = DEFAULT_BOOT_WINDOW_S) -> GatewayWatchdogStatus:
    """Determine whether this home has a live gateway, a booting one, or neither."""
    status = GatewayWatchdogStatus(home=str(home))
    try:
        from gateway.status import (
            gateway_boot_in_progress, gateway_slot_is_live, read_boot_marker,
        )
    except Exception as exc:  # pragma: no cover - import failure is a hard error
        status.reason = f"gateway.status unavailable: {exc}"
        return status

    def _slot_is_live(home_path: Path) -> Optional[int]:
        return gateway_slot_is_live(home_path / "gateway.pid")

    status.pid_file_present = (home / "gateway.pid").exists()
    try:
        # ``gateway_slot_is_live`` is the SUPERVISOR question ("is something
        # alive for this home?"), not the identity question — it consults the
        # runtime lock, the PID file AND the boot marker. A gateway that has
        # claimed the slot but is still minutes from serving is alive.
        gw_slot = _slot_is_live(home)
    except Exception:
        gw_slot = None
    if gw_slot is not None and gw_slot > 0:
        status.running_pid = gw_slot
    elif gw_slot is not None and gw_slot < 0:
        # The lock is held but the owner cannot be named.
        status.running_pid = None
        status.reason = "gateway runtime lock is held (owner PID unknown)"

    try:
        status.boot_in_progress = gateway_boot_in_progress(max_age_s=boot_window_s)
    except Exception:
        status.boot_in_progress = False

    marker = None
    try:
        marker = read_boot_marker()
    except Exception:
        marker = None
    if isinstance(marker, dict):
        status.boot_pid = marker.get("pid")
        status.boot_started_at = marker.get("boot_started_at")
        try:
            started = status.boot_started_at
            if started:
                from datetime import datetime, timezone

                age = (
                    datetime.now(timezone.utc) - datetime.fromisoformat(started)
                ).total_seconds()
                status.boot_age_s = round(age, 1)
        except (TypeError, ValueError):
            status.boot_age_s = None

    if status.boot_in_progress:
        # A boot in progress takes precedence in the wording: the PID may name
        # the booting process, but what a supervisor needs to know is that the
        # gateway has NOT finished starting yet — so it must not start another.
        age = f"{status.boot_age_s:.0f}s ago" if status.boot_age_s is not None else "unknown age"
        status.reason = f"gateway boot in progress (pid {status.boot_pid}, started {age})"
    elif status.running_pid is not None:
        status.reason = f"gateway pid {status.running_pid} is live"
    elif status.reason:
        # Already set (e.g. the lock is held but the owner cannot be named).
        pass
    elif status.pid_file_present:
        status.reason = "gateway.pid present but names no live gateway"
    else:
        status.reason = "no gateway running and none booting"
    return status


def _start_command() -> list[str]:
    """The platform's own way to start this home's gateway."""
    from hermes_cli.gateway import _installed_service_kind_for, _service_call  # noqa: F401

    kind = _installed_service_kind_for(True)
    if kind == "windows":
        return ["hermes", "gateway", "start"]
    if kind:
        return ["hermes", "gateway", "start"]
    return ["hermes", "gateway", "run"]


def ensure_gateway_running(
    home: Path,
    *,
    boot_window_s: int = DEFAULT_BOOT_WINDOW_S,
    start: bool = False,
    dry_run: bool = False,
) -> GatewayWatchdogStatus:
    """Start the gateway only when there is no live instance and no boot in progress.

    ``start=False`` (the default) makes this a pure probe: the caller is told
    what it would do, and nothing is launched. This mirrors the ``hermes kanban
    watchdog`` rule — the action that can cause harm is opt-in.
    """
    status = _probe_status(home, boot_window_s=boot_window_s)
    if status.healthy:
        return status
    if not start:
        status.reason += " (start withheld: --start not given)"
        return status
    if dry_run:
        status.reason += " (dry run: nothing started)"
        return status
    command = _start_command()
    try:
        proc = subprocess.run(command, capture_output=True, text=True, timeout=120)
        detail = (proc.stdout or proc.stderr or "").strip()
        status.reason = f"started via {' '.join(command)} (rc={proc.returncode}): {detail[:200]}"
    except Exception as exc:
        status.reason = f"start failed: {type(exc).__name__}: {exc}"
    return status


def format_status(status: GatewayWatchdogStatus) -> str:
    lines = [
        f"Gateway home : {status.home}",
        f"Running PID  : {status.running_pid if status.running_pid else '(none)'}",
        f"PID file     : {'present' if status.pid_file_present else 'absent'}",
        f"Booting     : {'yes (pid %s)' % status.boot_pid if status.boot_in_progress else 'no'}",
        f"State       : {'OK' if status.healthy else 'NO GATEWAY'}",
        f"Reason      : {status.reason}",
    ]
    return "\n".join(lines)


def _cmd_gateway_watchdog(args) -> int:
    """``hermes gateway watchdog`` entry point."""
    from hermes_cli.gateway import _print_lines

    home = _hermes_home()
    action = getattr(args, "watchdog_action", None) or "status"
    as_json = bool(getattr(args, "json", False))

    if action == "status":
        status = _probe_status(home)
        if as_json:
            print(json.dumps(status.to_dict(), indent=2))
        else:
            print(format_status(status))
        return 0 if status.healthy else 1

    if action == "ensure":
        status = ensure_gateway_running(
            home,
            boot_window_s=getattr(args, "boot_window_s", DEFAULT_BOOT_WINDOW_S),
            start=bool(getattr(args, "start", False)),
            dry_run=bool(getattr(args, "dry_run", False)),
        )
        if as_json:
            print(json.dumps(status.to_dict(), indent=2))
        else:
            print(format_status(status))
        return 0 if status.healthy else 1

    _print_lines(f"Unknown watchdog action: {action}")
    return 2
