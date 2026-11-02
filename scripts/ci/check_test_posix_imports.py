#!/usr/bin/env python3
"""Fail when a test module cannot be imported on Windows.

The failure this guards
-----------------------
Python evaluates, at IMPORT time: every module-level statement, every class
body, and every decorator / default-value / annotation argument. It does NOT
evaluate a function body until the function is called.

So a POSIX-only name in any of those import-time positions raises on Windows:

    tests/hermes_cli/test_doctor_journal_modes.py
        @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores ...")
        ^^^^^^^^^^^^^^^^^^^^^ AttributeError: module 'os' has no attribute 'geteuid'

    tests/tools/test_bot_turn_lock.py
        import fcntl
        ^^^^^ ModuleNotFoundError: No module named 'fcntl'

Both are pytest COLLECTION errors, and a collection error INTERRUPTS the whole
run: pytest exits 2 having collected and executed ZERO tests. A full-suite
run on Windows therefore looked green while testing nothing.

Why CI never saw it: the main ``tests`` lane is Linux-only, and the Windows
lane (``tests-os.yml``) narrows which FILES it imports using
``scripts/ci/list_os_marked_tests.py`` before applying ``-m``. Neither
offending file carries an OS marker, so the Windows lane never imported them.

How this checker works
----------------------
It does NOT try to reason about "is this reference evaluated at import time".
Instead it asks Python, by importing the candidate module in a subprocess:

``--native``     Import for real, on THIS host. On a real Windows runner this
                 IS the answer -- there is no simulation to be wrong about.

(default)        Import under a SIMULATED Windows host, so the defect is also
                 caught on a Linux runner:
                   * a ``sys.meta_path`` finder raises ModuleNotFoundError for
                     the POSIX-only stdlib modules (``fcntl``, ``termios``,
                     ``tty``, ``pwd``, ``grp``, ``resource``, ``posix``,
                     ``syslog``);
                   * the POSIX-only ``os`` attributes are deleted from ``os``;
                   * the host is faked (``os.name``, ``sys.platform``) so a
                     module that guards itself with ``os.name == "nt"`` takes
                     its Windows branch -- on a real Windows host that guard
                     short-circuits, so the reference it protects is never
                     evaluated.

Both modes use the same classifier, so they agree on what counts as a defect.

Cheapness
---------
Importing every test module is NOT an option (thousands of modules, minutes
per run). Instead the tree is pre-filtered with a cheap text scan for the
POSIX-only tokens, and only the handful of files that mention one are
imported. A module that never mentions a POSIX-only name cannot reference
one at import time.

Usage:
    python scripts/ci/check_test_posix_imports.py --all
    python scripts/ci/check_test_posix_imports.py --all --native
    python scripts/ci/check_test_posix_imports.py tests/tools/test_bot_turn_lock.py

Exit status:
    0 — every candidate test module imports cleanly on Windows
    1 — at least one candidate fails to import for a POSIX-only reason
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_ROOT = REPO_ROOT / "tests"

#: POSIX-only stdlib modules. Importing one on Windows raises ImportError.
POSIX_ONLY_MODULES = (
    "fcntl",
    "termios",
    "tty",
    "pwd",
    "grp",
    "resource",
    "posix",
    "syslog",
)

#: ``os`` attributes that do not exist on Windows. Dereferencing one raises
#: AttributeError. Measured against a real Windows host: every name below is
#: absent there and present on Linux.
POSIX_ONLY_OS_ATTRS = (
    "chown",
    "chroot",
    "confstr",
    "fchdir",
    "fchmod",
    "fchown",
    "fork",
    "forkpty",
    "fwalk",
    "getegid",
    "geteuid",
    "getgid",
    "getgroups",
    "getloadavg",
    "getpgid",
    "getpgrp",
    "getpriority",
    "getresgid",
    "getresuid",
    "getuid",
    "getxattr",
    "initgroups",
    "killpg",
    "lchown",
    "listxattr",
    "mkfifo",
    "mknod",
    "nice",
    "pathconf",
    "posix_spawn",
    "removexattr",
    "sched_get_priority_max",
    "sched_getaffinity",
    "sched_setaffinity",
    "setegid",
    "seteuid",
    "setgid",
    "setgroups",
    "setpgid",
    "setpgrp",
    "setregid",
    "setresgid",
    "setresuid",
    "setreuid",
    "setsid",
    "setuid",
    "setxattr",
    "sysconf",
    "uname",
    "wait4",
    "waitid",
)

#: Cheap text pre-filter. A file that mentions none of these tokens cannot
#: reference a POSIX-only name at import time, so it is never imported.
#: This keeps the check to a handful of modules instead of thousands.
_CANDIDATE_RE = re.compile(
    r"\b(?:" + "|".join(POSIX_ONLY_MODULES) + r")\b"
    r"|\bimport\s+tty\b"
    r"|\bos\s*\.\s*(?:" + "|".join(POSIX_ONLY_OS_ATTRS) + r")\b"
    r"|\bgetattr\s*\(\s*os\s*,\s*[\"'](?:" + "|".join(POSIX_ONLY_OS_ATTRS) + r")[\"']"
)

#: The probe that runs in the subprocess. It installs the Windows simulation
#: and imports each candidate module by file path.
#:
#: ``MODE`` is ``simulate`` (fake a Windows host -- used on a Linux runner)
#: or ``native`` (import for real on THIS host -- used on a real Windows
#: runner, where there is no simulation to be wrong about).
_PROBE = r'''
import importlib.util
import json
import os
import sys

BLOCKED_MODULES = %(blocked)s
WINDOWS_MISSING_OS_ATTRS = %(attrs)s

REPO = sys.argv[1]
MODULES = json.loads(sys.argv[2])
RESULTS_PATH = sys.argv[3]
MODE = sys.argv[4]

if REPO not in sys.path:
    sys.path.insert(0, REPO)


class _WindowsSimulator:
    """Makes POSIX-only stdlib modules unimportable, like on Windows.

    The exception type matters. Python raises ``ModuleNotFoundError`` (a
    subclass of ``ImportError``) for a module that is not on the path, and
    that is what a real Windows host raises for ``fcntl`` / ``pwd`` / etc.

    Raising a bare ``ImportError`` instead would change the behaviour of
    ``pytest.importorskip("fcntl")``: since pytest 9.1 that helper defaults
    to ``exc_type=ModuleNotFoundError``, so a bare ``ImportError`` would
    escape it and abort collection -- a failure that does NOT happen on a
    real Windows host, where the module genuinely is not found.
    """

    def __init__(self, blocked):
        self._blocked = blocked

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self._blocked:
            raise ModuleNotFoundError("No module named %%r" %% fullname, name=fullname)
        return None


# The POSIX-only modules are blocked ONLY while the module under test is
# being imported -- not for the whole process.
#
# Reason: on a POSIX host the STDLIB itself imports them. ``subprocess``
# does ``import fcntl`` at module level, ``shutil``/``pathlib`` reach for
# ``posix``-flavoured helpers, and the C accelerator modules do the same.
# Blocking them globally therefore breaks the standard library rather than
# the module under test, and every candidate would be reported as broken
# (182 false positives on Linux, where the guard was first exercised).
#
# What matters is whether the TEST MODULE can be imported on Windows, and
# the module under test is the only thing imported while the block is
# active. Anything it imports from the stdlib is imported by the stdlib's
# own code, which on a real Windows host has its own POSIX-free variants --
# so pre-importing the stdlib under test BEFORE installing the block makes
# the simulation faithful: the test module's own top-level ``import fcntl``
# still raises, while the stdlib keeps working.
#
# Concretely: the modules already in sys.modules (the stdlib and the
# repo's own dependencies, imported by the probe itself) stay usable; only a
# FRESH top-level import of a POSIX-only module raises.
#
# The POSIX-only modules are dropped from sys.modules BOTH before and after
# the stdlib is pre-imported. The second drop matters: on a POSIX host the
# stdlib pulls them in transitively (``subprocess`` does ``import fcntl`` at
# module level), and a module already in sys.modules is returned without
# consulting the finder -- so the test module's own ``import fcntl`` would
# silently succeed and the guard would never fire. (Found by the guard's own
# regression tests on Linux: the "must fire" cases passed on Windows and
# failed on Linux.)
for _name in list(sys.modules):
    if _name.split(".")[0] in BLOCKED_MODULES:
        del sys.modules[_name]

# Import the machinery a test module normally needs BEFORE the block goes
# in, so the block cannot break the stdlib out from under it.
import importlib.util  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import pathlib  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import unittest  # noqa: E402
import warnings  # noqa: E402

# Drop them again: the stdlib above may have pulled them in. The stdlib
# keeps working (it holds direct references to the module objects), while a
# fresh top-level import by the module under test now raises.
for _name in list(sys.modules):
    if _name.split(".")[0] in BLOCKED_MODULES:
        del sys.modules[_name]

# ── make the host LOOK like Windows ─────────────────────────────────────────
# The simulation has to fake the host, not just the module table. A module
# that guards itself with ``os.name == "nt"`` or ``sys.platform == "win32"``
# takes its POSIX branch on a Linux runner, so the POSIX-only reference it
# protects is evaluated and reported as a defect that does not exist on a
# real Windows host (where the guard short-circuits).
#
# Faking the host is exactly what this repo's test policy warns about for
# RUNTIME behaviour -- but here the fake is the POINT: the question is
# "would this module import on Windows", and the honest way to answer it is
# to make the interpreter's own view of the host match. The behaviour under
# test (import-time evaluation) is not faked.
#
# ``os.name`` is faked globally: nothing in the stdlib branches on it at
# import time, and it is the check the tree uses to mean "Windows".
#
# ``sys.platform`` is NOT faked globally. The stdlib DOES branch on it:
# asyncio picks the Windows event loop policy, psutil picks its backend, and
# on a POSIX host that means "cannot instantiate 'WindowsPath'" and
# "name 'base_events' is not defined" errors that have nothing to do with the
# module under test. Faking it broke the stdlib out from under the module,
# so the module failed to import for the wrong reason and the guard reported
# the wrong verdict. (Found by the guard's own regression tests on Linux.)
#
# ``os.name`` IS faked globally, but that has one consequence: ``pathlib``
# resolves ``PurePath`` to ``PureWindowsPath`` when ``os.name == "nt"``, and
# a PureWindowsPath cannot be instantiated on a POSIX host. So the Windows
# path classes are bound explicitly, which is exactly what the stdlib would
# have bound on a real Windows host.
#
# The POSIX-only MODULES are removed from ``sys.modules``, which is what
# makes ``import termios`` fail the way it does on Windows.
_REAL_OS_NAME = os.name
_REAL_PURE_PATH = None
if MODE == "simulate":
    os.name = "nt"

    try:
        import pathlib as _pathlib

        # On a real Windows host pathlib binds the Windows flavours at import
        # time. On a POSIX host it binds the POSIX ones, and ``PurePath`` --
        # whose concrete class is chosen per instantiation, from the CURRENT
        # os.name -- would resolve to PureWindowsPath, which REFUSES to be
        # instantiated on a non-Windows host ("NotImplementedError: cannot
        # instantiate 'WindowsPath' on your system"). We are pretending to
        # BE Windows, so that refusal is lifted: on a real Windows host
        # WindowsPath is instantiable.
        #
        # The refusal lives on the pathlib FLAVOUR (a module-level singleton
        # whose ``is_supported`` flag pathlib checks), not on the class.
        _REAL_PURE_PATH = _pathlib.PurePath
        _pathlib.PurePath = _pathlib.PureWindowsPath
        for _flavour in (
            getattr(_pathlib, "_windows_flavour", None),
            getattr(getattr(_pathlib, "WindowsPath", None), "_flavour", None),
        ):
            if _flavour is not None:
                try:
                    _flavour.is_supported = True
                except Exception:  # pragma: no cover - defensive
                    pass
    except Exception:  # pragma: no cover - defensive
        _REAL_PURE_PATH = None

    # Remove the os attributes Windows does not have. This is safe to do
    # globally: no stdlib module reaches for os.geteuid and friends at import
    # time, and it is exactly the shape of the defect being guarded.
    for _attr in WINDOWS_MISSING_OS_ATTRS:
        try:
            delattr(os, _attr)
        except AttributeError:
            pass

results = []
for _index, _rel in enumerate(MODULES):
    _path = os.path.join(REPO, _rel)
    _mod_name = "_windows_import_probe_%%d" %% _index
    _status = "ok"
    _detail = ""
    _simulator = _WindowsSimulator(BLOCKED_MODULES)
    if MODE == "simulate":
        sys.meta_path.insert(0, _simulator)
    try:
        try:
            _spec = importlib.util.spec_from_file_location(_mod_name, _path)
            _module = importlib.util.module_from_spec(_spec)
            sys.modules[_mod_name] = _module
            try:
                _spec.loader.exec_module(_module)
            finally:
                sys.modules.pop(_mod_name, None)
        except BaseException as _exc:  # noqa: BLE001 - a broken import is data
            _name = type(_exc).__name__
            _msg = str(_exc)
            _missing = getattr(_exc, "name", None)
            if isinstance(_exc, ImportError) and _missing in BLOCKED_MODULES:
                _status = "posix-module"
                _detail = "imports POSIX-only module %%r" %% _missing
            elif _name == "AttributeError" and "has no attribute" in _msg:
                _attr = _msg.rsplit("'", 2)[-2] if _msg.count("'") >= 2 else ""
                if _attr in WINDOWS_MISSING_OS_ATTRS and "module 'os'" in _msg:
                    _status = "posix-os-attr"
                    _detail = "references os.%%s at import time" %% _attr
                else:
                    _status = "other-error"
                    _detail = "%%s: %%s" %% (_name, _msg)
            elif _name in ("Skipped", "SkippedException"):
                # ``pytest.skip(..., allow_module_level=True)`` -- the repo's
                # canonical pattern for a POSIX-only module. That is a CLEAN
                # skip, not a broken import.
                _status = "skipped"
                _detail = "module-level skip: %%s" %% _msg
            else:
                _status = "other-error"
                _detail = "%%s: %%s" %% (_name, _msg)
    finally:
        # The block is scoped to the module under test. Without this the
        # POSIX-only modules stay unimportable for the rest of the probe,
        # which on a POSIX host breaks the stdlib for every later module.
        if MODE == "simulate":
            try:
                sys.meta_path.remove(_simulator)
            except ValueError:
                pass
    results.append({"module": _rel, "status": _status, "detail": _detail})

# Write to a FILE, not stdout: an imported test module may redirect stdout
# (a module-level ``sys.stdout = ...`` or a contextlib.redirect_stdout that
# is never unwound), which would swallow the marker and lose every result.
with open(RESULTS_PATH, "w", encoding="utf-8") as _fh:
    json.dump(results, _fh)

# Restore the real host before exiting, so nothing downstream of this probe
# in the same process sees a faked platform.
os.name = _REAL_OS_NAME
if _REAL_PURE_PATH is not None:
    _pathlib.PurePath = _REAL_PURE_PATH
'''


def candidate_files(paths: list[Path]) -> list[Path]:
    """Return the test files worth importing under the Windows simulation.

    A file that mentions none of the POSIX-only tokens cannot reference one
    at import time, so importing it would prove nothing.
    """
    found: list[Path] = []
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _CANDIDATE_RE.search(text):
            found.append(path)
    return sorted(found)


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _run_probe(paths: list[Path], mode: str) -> list[dict]:
    """Import each path in a subprocess and return one record per path.

    ``mode`` is ``simulate`` (fake a Windows host) or ``native`` (import for
    real on this host).
    """
    relatives = [_relative(p) for p in paths]
    script = _PROBE % {
        "blocked": repr(tuple(POSIX_ONLY_MODULES)),
        "attrs": repr(tuple(POSIX_ONLY_OS_ATTRS)),
    }
    # Sandbox HERMES_HOME: importing a test module must not touch the
    # operator's real ~/.hermes (the same reason tests/conftest.py sandboxes
    # it before any test module is imported).
    env = dict(os.environ)
    with tempfile.TemporaryDirectory(prefix="hermes-posix-import-probe-") as sandbox:
        env["HERMES_HOME"] = sandbox
        env["HERMES_TEST_ISOLATION"] = sandbox
        env.pop("HERMES_KANBAN_DB", None)
        env.pop("HERMES_KANBAN_BOARD", None)
        env.pop("HERMES_KANBAN_HOME", None)
        results_path = Path(sandbox) / "import-probe-results.json"
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(REPO_ROOT),
                json.dumps(relatives),
                str(results_path),
                mode,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=1800,
        )
        if not results_path.exists():
            raise RuntimeError(
                "the import probe produced no results "
                f"(exit {proc.returncode}):\n{proc.stderr[-4000:]}"
            )
        return json.loads(results_path.read_text(encoding="utf-8"))


def simulate_windows_imports(paths: list[Path]) -> list[dict]:
    """Import each path under a SIMULATED Windows host.

    Returns one record per path: ``{"module", "status", "detail"}`` where
    ``status`` is ``ok``, ``skipped``, ``posix-module``, ``posix-os-attr``
    or ``other-error``.
    """
    return _run_probe(paths, "simulate")


def native_imports(paths: list[Path]) -> list[dict]:
    """Import each path for REAL, on this host.

    On a real Windows host this IS the answer: no simulation, so nothing to
    be wrong about. On a POSIX host it is the Linux truth, which is a
    different question -- useful for cross-checking the simulation.
    """
    return _run_probe(paths, "native")


def check_paths(paths: list[Path]) -> tuple[list[dict], list[dict]]:
    """Return ``(defects, others)`` for the given files."""
    candidates = candidate_files(paths)
    if not candidates:
        return [], []
    records = simulate_windows_imports(candidates)
    defects = [r for r in records if r["status"].startswith("posix-")]
    others = [r for r in records if r["status"] == "other-error"]
    return defects, others

def _iter_python_files(roots: list[Path]) -> list[Path]:
    out: list[Path] = []
    for root in roots:
        if root.is_file():
            out.append(root)
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if d not in {"__pycache__", ".pytest_cache", ".git"}
            ]
            for fname in filenames:
                if fname.endswith(".py"):
                    out.append(Path(dirpath) / fname)
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fail when a test module cannot be imported on Windows "
            "(a collection error that aborts the whole run)."
        )
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Files or directories to check (default: the whole tests/ tree).",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Check the whole tests/ tree (the default when no path is given).",
    )
    parser.add_argument(
        "--native",
        action="store_true",
        help=(
            "Import for real on this host instead of simulating Windows. "
            "Authoritative when run on a real Windows host."
        ),
    )
    args = parser.parse_args(argv)

    if args.paths:
        roots = [p.resolve() for p in args.paths]
    else:
        roots = [TESTS_ROOT]

    files = _iter_python_files(roots)
    candidates = candidate_files(files)
    mode = "native" if args.native else "simulate"
    print(
        f"scanned {len(files)} Python file(s); "
        f"{len(candidates)} mention a POSIX-only name and are import-checked "
        f"({mode})"
    )
    if not candidates:
        print("✓ no test module references a POSIX-only name — nothing to import-check")
        return 0

    records = native_imports(candidates) if args.native else simulate_windows_imports(candidates)
    defects = [r for r in records if r["status"].startswith("posix-")]
    others = [r for r in records if r["status"] == "other-error"]

    for record in others:
        # Not a POSIX problem — a missing optional dependency or a genuinely
        # broken module. Surfaced so it is not silently swallowed, but it is
        # not this checker's business.
        print(f"note: {record['module']} failed to import for a non-POSIX reason: {record['detail']}")

    if not defects:
        print("✓ every candidate test module imports cleanly under the Windows simulation")
        return 0

    print("", file=sys.stderr)
    for record in defects:
        print(f"{record['module']}: {record['detail']}", file=sys.stderr)
    print(
        f"\n✗ {len(defects)} test module(s) cannot be imported on Windows.",
        file=sys.stderr,
    )
    print(
        "  A module that cannot be imported is a pytest COLLECTION error, and a\n"
        "  collection error INTERRUPTS the whole run: pytest exits 2 having run\n"
        "  ZERO tests. The Windows lane never sees it because it narrows which\n"
        "  files it imports by OS marker.\n"
        "  Fix: guard the reference (hasattr/getattr with a default, an\n"
        "  os.name/sys.platform check, a try/except ImportError around the\n"
        "  import) or skip the module on Windows with\n"
        "  pytest.skip('...', allow_module_level=True).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
