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
Instead it asks Python, by running a subprocess in which the host is
*simulated* to be Windows:

  * a ``sys.meta_path`` finder raises ImportError for the POSIX-only stdlib
    modules (``fcntl``, ``termios``, ``tty``, ``pwd``, ``grp``, ``resource``,
    ``posix``, ``syslog``);
  * the POSIX-only ``os`` attributes are deleted from the ``os`` module.

Importing a candidate module under that simulation reproduces EXACTLY what
happens on a real Windows host. A module that survives it imports cleanly on
Windows; a module that raises does not — and the collection error it causes
is named in the report.

Cheapness
---------
Importing every test module is NOT an option (thousands of modules, minutes
per run). Instead the tree is pre-filtered with a cheap text scan for the
POSIX-only tokens, and only the handful of files that mention one are
imported. A module that never mentions a POSIX-only name cannot reference
one at import time.

Usage:
    python scripts/ci/check_test_posix_imports.py --all
    python scripts/ci/check_test_posix_imports.py tests/tools/test_bot_turn_lock.py

Exit status:
    0 — every candidate test module imports cleanly under the Windows simulation
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

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in BLOCKED_MODULES:
            raise ModuleNotFoundError("No module named %%r" %% fullname, name=fullname)
        return None


for _name in list(sys.modules):
    if _name.split(".")[0] in BLOCKED_MODULES:
        del sys.modules[_name]
sys.meta_path.insert(0, _WindowsSimulator())

# Remove the os attributes Windows does not have.
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
    results.append({"module": _rel, "status": _status, "detail": _detail})

# Write to a FILE, not stdout: an imported test module may redirect stdout
# (a module-level ``sys.stdout = ...`` or a contextlib.redirect_stdout that
# is never unwound), which would swallow the marker and lose every result.
with open(RESULTS_PATH, "w", encoding="utf-8") as _fh:
    json.dump(results, _fh)
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


def simulate_windows_imports(paths: list[Path]) -> list[dict]:
    """Import each path under a simulated Windows host.

    Returns one record per path: ``{"module", "status", "detail"}`` where
    ``status`` is ``ok``, ``skipped``, ``posix-module``, ``posix-os-attr``
    or ``other-error``.
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
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=600,
        )
        if not results_path.exists():
            raise RuntimeError(
                "the Windows-simulation probe produced no results "
                f"(exit {proc.returncode}):\n{proc.stderr[-4000:]}"
            )
        return json.loads(results_path.read_text(encoding="utf-8"))


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
    args = parser.parse_args(argv)

    if args.paths:
        roots = [p.resolve() for p in args.paths]
    else:
        roots = [TESTS_ROOT]

    files = _iter_python_files(roots)
    candidates = candidate_files(files)
    print(
        f"scanned {len(files)} Python file(s); "
        f"{len(candidates)} mention a POSIX-only name and are import-checked"
    )
    if not candidates:
        print("✓ no test module references a POSIX-only name — nothing to import-check")
        return 0

    records = simulate_windows_imports(candidates)
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
