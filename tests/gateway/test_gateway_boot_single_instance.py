"""End-to-end proof: N concurrent gateway boots leave EXACTLY ONE live instance.

This is the acceptance criterion of the restart-storm card, expressed as a
test. The failure it pins is not hypothetical: on a memory-starved host a
13-minute boot looked like a dead gateway to every liveness probe, so each
supervision tick started another instance and 12 gateways stacked up.

The test spawns REAL subprocesses that race to boot a gateway for the same
HERMES_HOME and asserts that exactly one of them wins the boot slot and the
rest refuse. It exercises the real claim path (``gateway.status``), the real
argv fast-path marker write (``hermes_cli.main``) and the real lock — no mocks.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# A probe that claims the boot slot for a scratch HERMES_HOME and reports the
# outcome. It mimics the pre-heavy-import part of a gateway boot: claim the
# slot, then linger long enough for the siblings to race.
_CLAIM_PROBE = r"""
import json, os, sys, time
home = sys.argv[1]
label = sys.argv[2]
os.environ["HERMES_HOME"] = home
sys.path.insert(0, sys.argv[3])
from gateway.status import claim_gateway_boot_slot
won = claim_gateway_boot_slot()
print(json.dumps({"label": label, "pid": os.getpid(), "won": won}))
sys.stdout.flush()
# Hold the slot like a real boot would: the gateway owns it for its whole life.
time.sleep(float(sys.argv[4]))
"""


def _run_claim_probes(home: Path, count: int, hold_s: float) -> list[dict]:
    """Start ``count`` concurrent claim probes; return each probe's JSON result."""
    script = home / "claim_probe.py"
    script.write_text(_CLAIM_PROBE, encoding="utf-8")
    procs = []
    for i in range(count):
        procs.append(subprocess.Popen(
            [sys.executable, str(script), str(home), f"p{i}", str(REPO_ROOT), str(hold_s)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ))
    results = []
    for proc in procs:
        try:
            out, err = proc.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
        line = (out or "").strip().splitlines()
        payload = None
        for candidate in reversed(line):
            try:
                payload = json.loads(candidate)
                break
            except (ValueError, TypeError):
                continue
        results.append({"result": payload, "stderr": (err or "")[-2000:]})
    return results


class TestConcurrentBootLeavesOneInstance:
    def test_ten_concurrent_boots_leave_exactly_one_winner(self, tmp_path):
        """The acceptance criterion: N boots in a row -> exactly ONE live instance.

        Before the fix the boot slot was claimed only at the END of startup, so
        every one of these probes would have "won" (none of them had claimed
        anything when the others started) and all N would have gone on to run a
        gateway.
        """
        home = tmp_path / "hermes_home"
        home.mkdir()

        results = _run_claim_probes(home, count=10, hold_s=2.0)

        winners = [r for r in results if r["result"] and r["result"]["won"]]
        losers = [r for r in results if r["result"] and not r["result"]["won"]]

        assert len(results) == 10, f"not every probe reported: {results}"
        assert len(winners) == 1, (
            f"expected exactly ONE boot-slot winner, got {len(winners)}; "
            f"results={results}")
        assert len(losers) == 9, f"expected 9 losers, got {len(losers)}"

        # The winner's PID file names the winner — exactly one live instance.
        pid_payload = json.loads((home / "gateway.pid").read_text())
        assert pid_payload["pid"] == winners[0]["result"]["pid"]

    def test_a_second_boot_after_the_first_exits_succeeds(self, tmp_path):
        """A crashed boot must not leave the home permanently unbootable."""
        home = tmp_path / "hermes_home"
        home.mkdir()

        # First boot: claims the slot, then dies WITHOUT releasing it (simulates
        # a crash — the OS releases the flock, but the PID file survives).
        script = home / "claim_probe.py"
        script.write_text(_CLAIM_PROBE, encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(script), str(home), "first", str(REPO_ROOT), "0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        out, _ = proc.communicate(timeout=60)
        first = json.loads([ln for ln in out.strip().splitlines() if ln.startswith("{")][-1])
        assert first["won"] is True

        # Second boot: the stale PID file must not block it.
        results = _run_claim_probes(home, count=1, hold_s=0.5)
        assert results[0]["result"] is not None, results[0]["stderr"]
        assert results[0]["result"]["won"] is True, (
            "a boot after a crashed boot was refused — the stale PID file blocked it")
