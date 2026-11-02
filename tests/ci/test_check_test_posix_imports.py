"""Tests for ``scripts/ci/check_test_posix_imports.py``.

The guard exists because a test module that cannot be imported on Windows is
a pytest COLLECTION error, and a collection error INTERRUPTS the whole run:
pytest exits 2 having collected and executed ZERO tests. A full-suite run on
Windows therefore looked green while testing nothing.

So the contracts under test are the ones that decide a verdict:

* it FIRES on the two real defect shapes (a POSIX-only module-level import,
  and an ``os.<posix-only>`` reference evaluated at import time),
* it does NOT fire on the shapes that are already correct (a guarded
  reference, a module-level skip, ``pytest.importorskip``),
* and it does not flag the whole tree — the pre-filter keeps it cheap.

The defect shapes are verified against the REAL upstream cases that broke
collection, so the guard cannot pass by never firing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_test_posix_imports.py"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=REPO_ROOT,
    )


def _write(root: Path, relpath: str, body: str) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# ── the guard FIRES ─────────────────────────────────────────────────────────


def test_fires_on_a_posix_only_module_level_import(tmp_path):
    """``import fcntl`` at module level cannot even be imported on Windows."""
    _write(tmp_path, "test_broken.py", "import fcntl\n\n\ndef test_x():\n    pass\n")

    result = _run(str(tmp_path))

    assert result.returncode == 1
    assert "test_broken.py" in result.stderr
    assert "fcntl" in result.stderr


def test_fires_on_an_os_attribute_referenced_at_import_time(tmp_path):
    """The class-body skipif shape: the decorator ARGUMENT is evaluated when
    the class body executes — at import — so pytest cannot skip it away.
    """
    _write(
        tmp_path,
        "test_broken.py",
        "import os\n"
        "\n"
        "import pytest\n"
        "\n"
        "\n"
        "class TestThing:\n"
        '    @pytest.mark.skipif(os.geteuid() == 0, reason="root")\n'
        "    def test_x(self):\n"
        "        pass\n",
    )

    result = _run(str(tmp_path))

    assert result.returncode == 1
    assert "test_broken.py" in result.stderr
    assert "geteuid" in result.stderr


def test_fires_on_a_decorator_argument_at_import_time(tmp_path):
    """A decorator argument is evaluated at def time, like a class body."""
    _write(
        tmp_path,
        "test_broken.py",
        "import os\n"
        "\n"
        "import pytest\n"
        "\n"
        "\n"
        '@pytest.mark.skipif(os.geteuid() == 0, reason="root")\n'
        "def test_x():\n"
        "    pass\n",
    )

    result = _run(str(tmp_path))

    assert result.returncode == 1
    assert "geteuid" in result.stderr


# ── the guard does NOT fire ─────────────────────────────────────────────────


def test_does_not_fire_on_a_guarded_os_attribute(tmp_path):
    """``hasattr`` short-circuits, so the expression is safe to evaluate."""
    _write(
        tmp_path,
        "test_ok.py",
        "import os\n"
        "\n"
        "import pytest\n"
        "\n"
        "\n"
        '@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root")\n'
        "def test_x():\n"
        "    pass\n",
    )

    result = _run(str(tmp_path))

    assert result.returncode == 0, result.stderr


def test_does_not_fire_on_a_module_level_skip(tmp_path):
    """``pytest.skip(..., allow_module_level=True)`` is the repo's canonical
    pattern for a POSIX-only module — a clean skip, not a broken import."""
    _write(
        tmp_path,
        "test_ok.py",
        "import sys\n"
        "\n"
        "import pytest\n"
        "\n"
        'if sys.platform == "win32":\n'
        '    pytest.skip("POSIX-only", allow_module_level=True)\n'
        "\n"
        "import fcntl\n"
        "\n"
        "\n"
        "def test_x():\n"
        "    pass\n",
    )

    result = _run(str(tmp_path))

    assert result.returncode == 0, result.stderr


def test_does_not_fire_on_pytest_importorskip(tmp_path):
    """``pytest.importorskip("fcntl")`` skips the module cleanly on Windows.

    This one has a subtlety worth pinning: since pytest 9.1 the helper
    defaults to ``exc_type=ModuleNotFoundError``, so the simulation must
    raise that exact type — a bare ``ImportError`` would escape the helper
    and abort collection, reporting a defect that does not exist on a real
    Windows host.
    """
    _write(
        tmp_path,
        "test_ok.py",
        "import pytest\n"
        "\n"
        'fcntl = pytest.importorskip("fcntl")\n'
        "\n"
        "\n"
        "def test_x():\n"
        "    pass\n",
    )

    result = _run(str(tmp_path))

    assert result.returncode == 0, result.stderr


def test_does_not_fire_when_the_reference_is_inside_a_function_body(tmp_path):
    """A function body is evaluated at CALL time, not import time."""
    _write(
        tmp_path,
        "test_ok.py",
        "import os\n"
        "\n"
        "\n"
        "def test_x():\n"
        "    if hasattr(os, 'geteuid'):\n"
        "        pass\n",
    )

    result = _run(str(tmp_path))

    assert result.returncode == 0, result.stderr


def test_does_not_fire_on_a_file_with_no_posix_reference(tmp_path):
    """A file that mentions no POSIX-only name cannot reference one."""
    _write(tmp_path, "test_plain.py", "def test_x():\n    pass\n")

    result = _run(str(tmp_path))

    assert result.returncode == 0, result.stderr


def test_does_not_fire_on_an_os_name_guarded_reference(tmp_path):
    """``os.name == "nt" or os.geteuid() == 0`` short-circuits on Windows.

    The guard must not report the ``os.geteuid`` half, because on a real
    Windows host the ``os.name`` check comes first and the geteuid is never
    evaluated. This is the shape the tree actually uses
    (``tests/hermes_cli/test_plugins_cmd.py``).
    """
    _write(
        tmp_path,
        "test_ok.py",
        "import os\n"
        "\n"
        "import pytest\n"
        "\n"
        "\n"
        '@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="POSIX mode bits")\n'
        "def test_x():\n"
        "    pass\n",
    )

    result = _run(str(tmp_path))

    assert result.returncode == 0, result.stderr


def test_does_not_fire_on_a_sys_platform_guarded_reference(tmp_path):
    """``sys.platform == "win32" or os.geteuid() == 0`` also short-circuits."""
    _write(
        tmp_path,
        "test_ok.py",
        "import os\n"
        "import sys\n"
        "\n"
        "import pytest\n"
        "\n"
        "\n"
        '@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="POSIX mode bits")\n'
        "def test_x():\n"
        "    pass\n",
    )

    result = _run(str(tmp_path))

    assert result.returncode == 0, result.stderr


def test_the_simulation_does_not_break_the_standard_library(tmp_path):
    """The guard must judge the MODULE, not a broken stdlib.

    Faking the host on a POSIX runner can make the standard library
    unusable (``pathlib`` resolves to a WindowsPath that cannot be
    instantiated there; ``asyncio`` picks the Windows event loop policy).
    A module that then fails to import for THAT reason must not be reported
    as a POSIX-import defect -- the verdict would be wrong.
    """
    _write(
        tmp_path,
        "test_uses_stdlib.py",
        "import os\n"
        "import pathlib\n"
        "import subprocess\n"
        "import threading\n"
        "\n"
        "\n"
        "def test_x(tmp_path):\n"
        "    assert isinstance(pathlib.PurePath('a'), pathlib.PurePath)\n"
        "    assert subprocess.run(['echo', 'hi'], capture_output=True).returncode == 0\n",
    )

    result = _run(str(tmp_path))

    assert result.returncode == 0, result.stderr
    # And it must not be misreported as a POSIX problem either.
    assert "cannot be imported on Windows" not in result.stderr


# ── the pre-filter keeps it cheap ──────────────────────────────────────────


def test_only_files_mentioning_a_posix_name_are_import_checked(tmp_path):
    """The guard cannot import thousands of modules on every run, so the
    tree is pre-filtered by a cheap text scan first."""
    _write(tmp_path, "test_plain.py", "def test_x():\n    pass\n")
    _write(tmp_path, "test_mentions.py", "import fcntl\n")

    result = _run(str(tmp_path))

    assert "scanned 2 Python file(s)" in result.stdout
    assert "1 mention a POSIX-only name" in result.stdout


# ── the guard must not pass VACUOUSLY ───────────────────────────────────────


def test_fails_when_no_candidate_could_be_imported_at_all(tmp_path):
    """A check that judged nothing must not report success.

    Every candidate failing for a NON-POSIX reason (a missing dependency,
    most likely pytest) means no module was actually judged for Windows
    importability. Reporting success there is a false green -- and it is
    what happens in a CI lane that installs no dependencies.

    The candidate here mentions a POSIX-only name (so the cheap pre-filter
    selects it) but dies on a MISSING module that is not POSIX-only -- so it
    is not a defect, it is a module the probe could not import at all.
    """
    _write(
        tmp_path,
        "test_unimportable.py",
        "import a_module_that_does_not_exist_xyz\n"
        "\n"
        "\n"
        "def test_x():\n"
        "    import fcntl  # noqa: F401 - only here to pass the pre-filter\n",
    )

    result = _run(str(tmp_path))

    # The guard must refuse to claim success, and must say why.
    assert result.returncode == 2, result.stderr
    assert "proved nothing" in result.stderr


def test_the_vacuous_case_is_distinct_from_a_real_defect(tmp_path):
    """Exit code 2 (nothing judged) is deliberately different from exit 1
    (a module really cannot be imported on Windows), so the two are told
    apart by a CI consumer."""
    vacuous_dir = tmp_path / "vacuous"
    _write(
        vacuous_dir,
        "test_unimportable.py",
        "import a_module_that_does_not_exist_xyz\n"
        "\n"
        "\n"
        "def test_x():\n"
        "    import fcntl  # noqa: F401 - only here to pass the pre-filter\n",
    )
    real_dir = tmp_path / "real"
    _write(real_dir, "test_posix.py", "import fcntl\n\n\ndef test_x():\n    pass\n")

    vacuous = _run(str(vacuous_dir))
    real = _run(str(real_dir))

    assert vacuous.returncode == 2
    assert real.returncode == 1  # a genuine POSIX-only import
    assert "proved nothing" in vacuous.stderr
    assert "proved nothing" not in real.stderr


# ── the real tree ───────────────────────────────────────────────────────────


def test_the_real_test_tree_passes_the_guard():
    """The whole point: the tree as it stands must be import-clean on Windows.

    This is the check the lint lane runs. If it fails, some module has
    reintroduced an import-time POSIX reference.
    """
    result = _run("--all")

    assert result.returncode == 0, result.stderr


def test_the_two_modules_that_broke_collection_are_importable():
    """The two upstream defect cases, by name.

    Both were collection errors on Windows: the doctor test's class-body
    ``os.geteuid()`` and the bot-turn-lock module's ``import fcntl``. They
    are pinned individually so a regression names the file instead of
    failing one big tree-wide assertion.
    """
    result = _run(
        "tests/hermes_cli/test_doctor_journal_modes.py",
        "tests/tools/test_bot_turn_lock.py",
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "relpath",
    [
        "tests/hermes_cli/test_doctor_journal_modes.py",
        "tests/tools/test_bot_turn_lock.py",
    ],
)
def test_the_fixed_modules_collect_on_this_host(relpath):
    """A collection error is exit 2; a clean skip is exit 5; success is 0.

    Asserted on the host the tests run on, so the fix is proven where the
    board's workers actually run.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", relpath, "--collect-only", "-q"],
        capture_output=True,
        text=True,
        timeout=600,
        cwd=REPO_ROOT,
    )

    assert result.returncode != 2, (
        f"{relpath} still fails at COLLECTION on this host:\n{result.stdout[-2000:]}"
    )
