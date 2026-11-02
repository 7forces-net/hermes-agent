"""Tests for hermes_cli.gateway_windows."""

import logging
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import hermes_cli.gateway as gateway
import hermes_cli.gateway_windows as gateway_windows
import hermes_cli.setup as setup


_BREAKAWAY_MARKER = "_HERMES_GATEWAY_BREAKAWAY"




def test_schtasks_encoding_falls_back_to_utf8(monkeypatch):
    """A broken/empty locale must not leave us without a decoder (issue #38172)."""

    monkeypatch.setattr(gateway_windows.locale, "getpreferredencoding", lambda *a, **k: "")
    assert gateway_windows._schtasks_encoding() == "utf-8"

    def _boom(*args, **kwargs):
        raise RuntimeError("locale exploded")

    monkeypatch.setattr(gateway_windows.locale, "getpreferredencoding", _boom)
    assert gateway_windows._schtasks_encoding() == "utf-8"




@pytest.mark.windows_only
def test_build_gateway_argv_keeps_venv_console_python_for_uv_venv(monkeypatch, tmp_path):
    """No pythonw / base-interpreter detour: the venv console python.exe is
    launched hidden (CREATE_NO_WINDOW) so descendants inherit its hidden
    console instead of flashing their own (#54220/#56747).

    Windows-only: ``_build_gateway_argv()`` asserts the host is Windows and the
    argv/env overlay it returns is built from real Windows path separators and
    ``Scripts/python.exe`` layout — a patched ``sys.platform`` covered the
    branch but not any of that.
    """

    project = tmp_path / "project"
    scripts = project / "venv" / "Scripts"
    site_packages = project / "venv" / "Lib" / "site-packages"
    hermes_home = tmp_path / "hermes-home"
    base = tmp_path / "uv" / "python" / "cpython-3.11-windows-x86_64-none"
    scripts.mkdir(parents=True)
    site_packages.mkdir(parents=True)
    hermes_home.mkdir()
    base.mkdir(parents=True)

    venv_python = scripts / "python.exe"
    venv_pythonw = scripts / "pythonw.exe"
    base_pythonw = base / "pythonw.exe"
    for exe in (venv_python, venv_pythonw, base_pythonw):
        exe.write_text("", encoding="utf-8")
    (project / "venv" / "pyvenv.cfg").write_text(
        f"home = {base}\nimplementation = CPython\nuv = 0.11.14\nversion_info = 3.11.15\n",
        encoding="utf-8",
    )

    import hermes_cli.gateway as gateway

    monkeypatch.setattr(gateway, "PROJECT_ROOT", project)
    monkeypatch.setattr(gateway, "get_python_path", lambda: str(venv_python))
    monkeypatch.setattr(gateway, "_profile_arg", lambda hermes_home: "")
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: str(hermes_home))

    argv, cwd, env_overlay = gateway_windows._build_gateway_argv()

    assert argv[:3] == [str(venv_python), "-m", "hermes_cli.main"]
    assert cwd == str(hermes_home.resolve())
    assert env_overlay["VIRTUAL_ENV"] == str(project / "venv")
    assert str(project) in env_overlay["PYTHONPATH"].split(gateway_windows.os.pathsep)


@pytest.mark.windows_only
def test_spawn_detached_marks_primary_breakaway_success(monkeypatch, tmp_path, caplog):
    """A successful breakaway spawn reports true without a warning."""
    argv = ["python.exe", "-m", "hermes_cli.main", "gateway", "run"]
    cwd = str(tmp_path)
    calls = []

    def fake_popen(call_argv, **kwargs):
        calls.append((call_argv, kwargs))
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(
        gateway_windows,
        "_build_gateway_argv",
        lambda home=None: (argv, cwd, {"HERMES_GATEWAY_DETACHED": "1"}),
    )
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gateway_windows.subprocess, "Popen", fake_popen)
    caplog.set_level(logging.WARNING, logger=gateway_windows.__name__)

    assert gateway_windows._spawn_detached() == 12345
    assert len(calls) == 1
    actual_argv, kwargs = calls[0]
    assert actual_argv == argv
    assert kwargs["cwd"] == cwd
    assert kwargs["creationflags"] == gateway_windows.windows_detach_flags()
    assert kwargs["env"][_BREAKAWAY_MARKER] == "1"
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is kwargs["stderr"]
    assert not caplog.records


@pytest.mark.windows_only
def test_spawn_detached_warns_and_marks_no_breakaway_fallback(
    monkeypatch, tmp_path, caplog
):
    """A denied breakaway retries once with private false metadata."""
    argv = ["python.exe", "-m", "hermes_cli.main", "gateway", "run"]
    cwd = str(tmp_path)
    calls = []

    def fake_popen(call_argv, **kwargs):
        calls.append((call_argv, kwargs))
        if len(calls) == 1:
            error = OSError(13, "Access is denied")
            error.winerror = 5
            raise error
        return SimpleNamespace(pid=23456)

    monkeypatch.setattr(
        gateway_windows,
        "_build_gateway_argv",
        lambda home=None: (
            argv,
            cwd,
            {"HERMES_GATEWAY_DETACHED": "1", "SECRET_SENTINEL": "do-not-log"},
        ),
    )
    monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gateway_windows.subprocess, "Popen", fake_popen)
    caplog.set_level(logging.WARNING, logger=gateway_windows.__name__)

    assert gateway_windows._spawn_detached() == 23456
    assert len(calls) == 2
    (argv_primary, primary), (argv_fallback, fallback) = calls
    assert argv_primary == argv_fallback == argv
    assert primary["cwd"] == fallback["cwd"] == cwd
    assert primary["creationflags"] == gateway_windows.windows_detach_flags()
    assert (
        fallback["creationflags"]
        == gateway_windows.windows_detach_flags_without_breakaway()
    )
    assert primary["stdin"] is fallback["stdin"] is subprocess.DEVNULL
    assert primary["stdout"] is primary["stderr"]
    assert fallback["stdout"] is fallback["stderr"]
    assert Path(primary["stdout"].name) == Path(fallback["stdout"].name)
    assert primary["close_fds"] is fallback["close_fds"] is True
    assert primary["env"] is not fallback["env"]
    assert primary["env"][_BREAKAWAY_MARKER] == "1"
    assert fallback["env"][_BREAKAWAY_MARKER] == "0"
    assert {
        key: value for key, value in primary["env"].items() if key != _BREAKAWAY_MARKER
    } == {
        key: value for key, value in fallback["env"].items() if key != _BREAKAWAY_MARKER
    }

    warnings = [
        record for record in caplog.records if record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "5" in warnings[0].getMessage()
    assert "do-not-log" not in warnings[0].getMessage()
    assert str(tmp_path) not in warnings[0].getMessage()


class TestStableWindowsGatewayWorkingDir:
    def test_stable_gateway_working_dir_uses_hermes_home(self, tmp_path, monkeypatch):
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: home)
        assert gateway_windows._stable_gateway_working_dir(tmp_path / "checkout") == str(home.resolve())

    def test_stable_gateway_working_dir_falls_back_to_project_root(self, tmp_path, monkeypatch):
        missing = tmp_path / "missing" / ".hermes"
        project = tmp_path / "checkout"
        monkeypatch.setattr("hermes_cli.config.get_hermes_home", lambda: missing)
        assert gateway_windows._stable_gateway_working_dir(project) == str(project)




def _arrange_startup_fallback(monkeypatch, tmp_path, running_pids):
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    startup_entry = tmp_path / "Startup" / "Hermes_Gateway_alice.cmd"
    calls = []

    monkeypatch.setattr(gateway_windows, "_prompt_install_choices", lambda *args, **kwargs: (False, True))
    monkeypatch.setattr(gateway_windows, "_assert_windows", lambda: None)
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway_alice")
    monkeypatch.setattr(gateway_windows, "_write_task_script", lambda: script_path)
    monkeypatch.setattr(
        gateway_windows,
        "_install_scheduled_task",
        lambda task_name, script_path: (
            False,
            "schtasks /Create failed (code 1): ERROR: Access is denied.",
        ),
    )
    monkeypatch.setattr(gateway_windows, "_should_fall_back", lambda code, detail: True)
    monkeypatch.setattr(gateway_windows, "_is_running_as_admin", lambda: True)
    monkeypatch.setattr(
        gateway_windows,
        "_launch_elevated_install",
        lambda force=False, start_now=None, start_on_login=None: calls.append(("elevate", force, start_now, start_on_login)) or True,
    )

    def fake_install_startup_entry(path: Path) -> Path:
        calls.append(("install_startup", path))
        return startup_entry

    monkeypatch.setattr(gateway_windows, "_install_startup_entry", fake_install_startup_entry)
    monkeypatch.setattr(gateway_windows, "_spawn_detached", lambda path: calls.append(("spawn", path)) or 12345)
    monkeypatch.setattr(gateway_windows, "_report_gateway_start", lambda via: calls.append(("report_start", via)))
    monkeypatch.setattr(gateway_windows, "_print_next_steps", lambda: calls.append(("next_steps", None)))
    monkeypatch.setattr(gateway, "find_gateway_pids", lambda: running_pids)
    monkeypatch.setattr(gateway, "_profile_arg", lambda: "--profile alice")
    return script_path, calls




@pytest.mark.windows_only
def test_elevated_gateway_command_uses_hidden_console_python(monkeypatch):
    """UAC handoff launches console python with SW_HIDE — a single hidden
    console, not console-less pythonw (#54220/#56747), and no visible
    elevated cmd.exe window left open.

    Windows-only: the code path runs behind ``_assert_windows()`` and goes
    through ``ctypes.windll.shell32``, neither of which exists on a faked
    host. ShellExecuteW itself stays mocked — it would raise a real UAC
    prompt — but the host identity is genuine.
    """
    calls = []

    class FakeShell32:
        def ShellExecuteW(self, hwnd, verb, executable, params, cwd, show):
            calls.append((hwnd, verb, executable, params, cwd, show))
            return 33

    class FakeWindll:
        shell32 = FakeShell32()

    monkeypatch.setattr(gateway_windows, "_current_profile_cli_args", lambda: ["--profile", "alice"])
    monkeypatch.setattr(gateway_windows.sys, "executable", r"C:\Hermes\venv\Scripts\python.exe")
    monkeypatch.setattr(gateway_windows.ctypes, "windll", FakeWindll(), raising=False)

    assert gateway_windows._launch_elevated_gateway_command("install", ["--start-now", "--elevated-handoff"])

    assert len(calls) == 1
    _hwnd, verb, executable, params, cwd, show = calls[0]
    assert verb == "runas"
    assert executable == r"C:\Hermes\venv\Scripts\python.exe"
    assert "--profile alice gateway install --start-now --elevated-handoff" in params
    assert show == 0
    assert cwd


def test_install_scheduled_task_recreates_instead_of_change(monkeypatch, tmp_path):
    """Install must CREATE (not ``/Change``) so stale minute-repeat task settings are not preserved.

    Host-agnostic on purpose: ``_install_scheduled_task`` only renders the task
    XML and shells out through ``_exec_schtasks`` (mocked here as the genuine
    external dependency), so no platform fake is needed.
    """
    calls = []
    script_path = tmp_path / "Hermes_Gateway_alice.cmd"
    xml_seen = {}

    monkeypatch.setattr(gateway_windows, "_resolve_task_user", lambda: r"DOMAIN\\alice")

    def fake_schtasks(args):
        calls.append(tuple(args))
        if args[0] == "/Delete":
            return (0, "SUCCESS", "")
        if args[0] == "/Create":
            xml_path = Path(args[args.index("/XML") + 1])
            xml_seen["text"] = xml_path.read_text(encoding="utf-16")
            return (0, "SUCCESS", "")
        if args[0] == "/Query":
            return (1, "", "ERROR: The system cannot find the file specified.")
        raise AssertionError(f"unexpected schtasks args: {args}")

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)
    ok, detail = gateway_windows._install_scheduled_task("Hermes_Gateway_alice", script_path)

    assert ok is True
    assert "/Change" not in [arg for call in calls for arg in call]
    # CREATE first: "/Create /F" replaces the definition in place, so a delete-first
    # install would destroy a working supervisor whenever the create then failed.
    assert calls[0][0] == "/Create"
    assert calls[0][:4] == ("/Create", "/F", "/TN", "Hermes_Gateway_alice")
    assert "/XML" in calls[0]
    assert "/SC" not in calls[0]
    assert "<Delay>PT30S</Delay>" in xml_seen["text"]
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml_seen["text"]
    assert "<StopOnIdleEnd>false</StopOnIdleEnd>" in xml_seen["text"]
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml_seen["text"]
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml_seen["text"]
    assert "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>" in xml_seen["text"]
    assert "<RestartOnFailure>" in xml_seen["text"]
    assert "<Count>999</Count>" in xml_seen["text"]
    # Scheduled Task launches the console-less .vbs via wscript.exe, never cmd.exe
    # (issue #45599 fix A: no console -> no logon CTRL_CLOSE_EVENT / 0xC000013A).
    assert "<Command>wscript.exe</Command>" in xml_seen["text"]
    assert "//B //Nologo" in xml_seen["text"]
    assert "Hermes_Gateway_alice.vbs" in xml_seen["text"]
    assert "cmd.exe" not in xml_seen["text"]


def test_logon_trigger_names_the_user_so_an_unelevated_user_can_register_it():
    """The logon trigger must carry a ``<UserId>`` — a bare trigger means "at every
    user's logon", which only an administrator may register.

    A logon trigger with no ``<UserId>`` is refused for the unelevated account:
    ``schtasks /Create /XML`` -> "ERROR: Access is denied." The ``<Principal>``'s
    ``<UserId>`` does NOT scope the trigger — measured A/B, one element changed.
    """
    xml = gateway_windows._build_scheduled_task_xml(
        r"\Hermes\Hermes_Gateway", Path(r"C:\Hermes\gateway-service\Hermes_Gateway.vbs"), r"DOMAIN\alice",
    )
    trigger = xml.split("<LogonTrigger>", 1)[1].split("</LogonTrigger>", 1)[0]
    assert "<UserId>DOMAIN\\alice</UserId>" in trigger
    # ...and the principal still names the user.
    principal = xml.split("<Principal", 1)[1].split("</Principal>", 1)[0]
    assert "<UserId>DOMAIN\\alice</UserId>" in principal


def test_logon_trigger_falls_back_to_the_account_sid(monkeypatch):
    """A user that cannot be resolved by name still gets a NAMED trigger, never a bare one."""
    monkeypatch.setattr(gateway_windows, "_resolve_current_user_sid", lambda: "S-1-5-21-1-2-1001")
    monkeypatch.delenv("USERNAME", raising=False)
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("LOGNAME", raising=False)

    assert gateway_windows._resolve_task_user() == "S-1-5-21-1-2-1001"
    xml = gateway_windows._build_scheduled_task_xml(
        r"\Hermes\Hermes_Gateway", Path(r"C:\Hermes\gateway-service\Hermes_Gateway.vbs"),
        gateway_windows._resolve_task_user(),
    )
    trigger = xml.split("<LogonTrigger>", 1)[1].split("</LogonTrigger>", 1)[0]
    assert "<UserId>S-1-5-21-1-2-1001</UserId>" in trigger


def test_failed_install_does_not_delete_the_working_task(monkeypatch, tmp_path):
    """A create refused for PERMISSION must never delete the existing task.

    On this host ``/Delete`` succeeds where ``/Create`` is denied, so the old
    delete-first install destroyed a working supervisor and then reported the
    failure — leaving the host with no supervisor at all.
    """
    calls = []
    script_path = tmp_path / "Hermes_Gateway.cmd"

    monkeypatch.setattr(gateway_windows, "_resolve_task_user", lambda: r"DOMAIN\alice")

    def fake_schtasks(args):
        calls.append(tuple(args))
        if args[0] == "/Create":
            return (1, "", "ERROR: Access is denied.")
        if args[0] == "/Delete":
            return (0, "SUCCESS: The scheduled task was successfully deleted.", "")
        raise AssertionError(f"unexpected schtasks args: {args}")

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)
    ok, detail = gateway_windows._install_scheduled_task(r"\Hermes\Hermes_Gateway", script_path)

    assert ok is False
    assert "Access is denied" in detail
    assert not [call for call in calls if call[0] == "/Delete"], (
        "a permission failure must not delete the working task"
    )


def test_corrupt_task_is_deleted_then_recreated(monkeypatch, tmp_path):
    """A non-permission create failure (a corrupt task) may delete and retry once."""
    calls = []
    script_path = tmp_path / "Hermes_Gateway.cmd"

    monkeypatch.setattr(gateway_windows, "_resolve_task_user", lambda: r"DOMAIN\alice")

    def fake_schtasks(args):
        calls.append(tuple(args))
        if args[0] == "/Create":
            # First create fails as corrupt; the retry (after the delete) succeeds.
            create_calls = [c for c in calls if c[0] == "/Create"]
            if len(create_calls) == 1:
                return (1, "", "ERROR: The task XML contains a value which is incorrectly formatted.")
            return (0, "SUCCESS", "")
        if args[0] == "/Delete":
            return (0, "SUCCESS", "")
        if args[0] == "/Query":
            return (1, "", "ERROR: The system cannot find the file specified.")
        raise AssertionError(f"unexpected schtasks args: {args}")

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)
    ok, detail = gateway_windows._install_scheduled_task(r"\Hermes\Hermes_Gateway", script_path)

    assert ok is True
    # create (corrupt) -> delete -> create (ok) -> query the legacy root task
    assert [c[0] for c in calls] == ["/Create", "/Delete", "/Create", "/Query"]
    assert calls[1][-1] == "\\Hermes\\Hermes_Gateway"
    assert calls[3][-1] == "Hermes_Gateway"


def test_install_never_uses_the_password_prompting_ru_variant(monkeypatch, tmp_path):
    """``/RU <user> /NP /IT`` makes schtasks PROMPT for a password and then fail; it can
    never succeed and the swallowed prompt turns the attempt into a silent hang."""
    calls = []
    script_path = tmp_path / "Hermes_Gateway.cmd"

    monkeypatch.setattr(gateway_windows, "_resolve_task_user", lambda: r"DOMAIN\alice")

    def fake_schtasks(args):
        calls.append(tuple(args))
        if args[0] == "/Create":
            return (0, "SUCCESS", "")
        if args[0] == "/Query":
            return (1, "", "ERROR: The system cannot find the file specified.")
        raise AssertionError(f"unexpected schtasks args: {args}")

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)
    ok, _detail = gateway_windows._install_scheduled_task(r"\Hermes\Hermes_Gateway", script_path)

    assert ok is True
    assert not [arg for call in calls for arg in call if arg == "/RU"]
    assert not [arg for call in calls for arg in call if arg == "/NP"]


def test_schtasks_lookups_use_the_folder_qualified_task_name(monkeypatch):
    """A task in the ``\\Hermes`` folder is NOT addressed by its bare leaf name.

    Measured: ``schtasks /Query /TN Hermes_Gateway`` returns "cannot find the file" for
    the task registered at ``\\Hermes\\Hermes_Gateway``. Every schtasks argv must
    therefore carry the folder, or it silently addresses nothing.

    ``is_task_registered()`` also probes the bare leaf, but only as the LEGACY
    root-level spelling it may have to clean up — the AUTHORITATIVE lookup is the
    folder-qualified one.
    """
    queried = []
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway")
    monkeypatch.setattr(gateway_windows, "get_task_path", lambda: "\\Hermes\\")

    def fake_schtasks(args):
        queried.append(tuple(args))
        return (1, "", "ERROR: The system cannot find the file specified.")

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)
    assert gateway_windows.get_task_schtasks_name() == "\\Hermes\\Hermes_Gateway"

    gateway_windows.is_task_registered()
    gateway_windows.query_task_status()
    assert gateway_windows.is_task_registered() is False

    qualified = [a for a in queried if a[0] == "/Query" and "\\Hermes\\Hermes_Gateway" in a]
    assert qualified, "the authoritative lookup must use the folder-qualified name"
    # The bare leaf is only ever probed as the legacy root-level spelling.
    for args in queried:
        if args[0] == "/Query" and args[-1] == "Hermes_Gateway":
            assert len(args) == 3, "the bare-leaf probe must be a plain /Query /TN <leaf>"


def test_is_task_registered_finds_the_folder_qualified_task(monkeypatch):
    """The authoritative task lives at ``\\Hermes\\Hermes_Gateway``; the bare-leaf query
    does NOT find it (measured: "The system cannot find the file specified.")."""
    monkeypatch.setattr(gateway_windows, "get_task_name", lambda: "Hermes_Gateway")
    monkeypatch.setattr(gateway_windows, "get_task_path", lambda: "\\Hermes\\")

    def fake_schtasks(args):
        if args[0] == "/Query" and args[-1] == "\\Hermes\\Hermes_Gateway":
            return (0, "TaskName: \\Hermes\\Hermes_Gateway", "")
        return (1, "", "ERROR: The system cannot find the file specified.")

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)
    assert gateway_windows.is_task_registered() is True


def test_root_level_task_of_the_same_leaf_is_removed_after_a_folder_install(monkeypatch, tmp_path):
    """A root-level task of the same leaf name is the SAME gateway's second launcher.

    Two launchers means two messaging pollers on one token and two writers on one
    kanban.db — the hazard the parent card t_75aa0341 called out. The install must
    remove it.
    """
    calls = []
    script_path = tmp_path / "Hermes_Gateway.cmd"

    monkeypatch.setattr(gateway_windows, "_resolve_task_user", lambda: r"DOMAIN\alice")

    def fake_schtasks(args):
        calls.append(tuple(args))
        if args[0] == "/Create":
            return (0, "SUCCESS", "")
        if args[0] == "/Delete":
            return (0, "SUCCESS", "")
        if args[0] == "/Query":
            # The root-level task of the same leaf EXISTS.
            if args[-1] == "Hermes_Gateway":
                return (0, "TaskName: Hermes_Gateway", "")
            return (1, "", "ERROR: The system cannot find the file specified.")
        raise AssertionError(f"unexpected schtasks args: {args}")

    monkeypatch.setattr(gateway_windows, "_exec_schtasks", fake_schtasks)
    ok, _detail = gateway_windows._install_scheduled_task(r"\Hermes\Hermes_Gateway", script_path)

    assert ok is True
    deletes = [call for call in calls if call[0] == "/Delete"]
    assert deletes, "the legacy root-level task must be deleted"
    assert deletes[0][-1] == "Hermes_Gateway"


def test_gateway_vbs_script_is_console_less(monkeypatch):
    """The .vbs launcher must avoid cmd.exe entirely and Run pythonw hidden
    (issue #45599 fix A: no console -> no logon CTRL_CLOSE_EVENT / 0xC000013A)."""
    monkeypatch.setattr(
        gateway_windows,
        "_resolve_detached_python",
        lambda exe: (r"C:\venv\Scripts\pythonw.exe", Path(r"C:\venv"), []),
    )
    content = gateway_windows._build_gateway_vbs_script(
        r"C:\venv\Scripts\python.exe",
        r"C:\Hermes",
        r"C:\Hermes",
        "--profile work",
    )
    assert "cmd.exe" not in content.lower()
    assert 'CreateObject("WScript.Shell")' in content
    assert "pythonw.exe" in content
    assert "hermes_cli.main" in content
    assert "gateway run" in content
    assert ", 0, False" in content  # hidden window, detached/async
    for var in ("HERMES_HOME", "PYTHONIOENCODING", "HERMES_GATEWAY_DETACHED", "VIRTUAL_ENV", "PYTHONPATH"):
        assert var in content
    assert "--profile" in content and "work" in content
    assert content.endswith("\r\n")














# ---------------------------------------------------------------------------
# stop() drain semantics — issue #33778
#
# Background: on Windows, asyncio.add_signal_handler raises NotImplementedError,
# so the gateway's SIGTERM handler (which drains in-flight agents and writes
# resume_pending=True) never fires when `hermes gateway stop` kills the
# process. The fix: stop() writes the planned_stop_marker first, waits for
# the gateway's marker-watcher thread to drain + exit cleanly, then escalates
# to taskkill if drain times out.
# ---------------------------------------------------------------------------








