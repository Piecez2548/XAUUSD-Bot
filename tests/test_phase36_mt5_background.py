from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

from config.settings import Settings, load_settings
from mt5.bootstrap import MT5AutoLauncher, MT5Verification, TerminalProcess
from mt5.window_mode import MT5BackgroundWindowController, ProcessIdentity
from persistence.database import Database
from services.control import TelegramControlService


class FakeWindowAdapter:
    def __init__(self) -> None:
        self.identities: dict[int, ProcessIdentity] = {}
        self.windows: dict[int, tuple[int, ...]] = {}
        self.owners: dict[int, int] = {}
        self.visible: dict[int, bool] = {}
        self.hidden: list[int] = []
        self.fail_hide: Exception | None = None
        self.on_enumerate = None

    def process_identity(self, pid: int) -> ProcessIdentity | None:
        return self.identities.get(pid)

    def top_level_windows(self, pid: int) -> tuple[int, ...]:
        if self.on_enumerate is not None:
            self.on_enumerate()
        return self.windows.get(pid, ())

    def window_owner_pid(self, hwnd: int) -> int | None:
        return self.owners.get(hwnd)

    def is_window_visible(self, hwnd: int) -> bool:
        return self.visible.get(hwnd, False)

    def hide_window(self, hwnd: int) -> None:
        self.hidden.append(hwnd)
        if self.fail_hide is not None:
            raise self.fail_hide
        self.visible[hwnd] = False


def _identity(pid: int, path: Path, created: int = 10) -> ProcessIdentity:
    return ProcessIdentity(pid, str(path), created)


def _controller(adapter, path, pids, **kwargs):
    kwargs.setdefault("retry_seconds", 0)
    return MT5BackgroundWindowController(
        enabled=True,
        configured_path=str(path),
        process_probe=lambda: tuple(TerminalProcess(pid) for pid in pids()),
        adapter=adapter,
        windows=True,
        **kwargs,
    )


def test_disabled_background_mode_does_not_use_windows_adapter(tmp_path: Path) -> None:
    controller = MT5BackgroundWindowController(
        enabled=False,
        configured_path=str(tmp_path / "terminal64.exe"),
        process_probe=lambda: (),
        windows=True,
    )
    controller.start()
    assert controller.status == {
        "enabled": False,
        "state": "DISABLED",
        "pid": None,
        "last_error_type": None,
    }


def test_background_mode_configuration_is_opt_in(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MT5_BACKGROUND_MODE", raising=False)
    assert load_settings(tmp_path / "missing.env").mt5_background_mode is False
    monkeypatch.setenv("MT5_BACKGROUND_MODE", "true")
    assert load_settings(tmp_path / "missing.env").mt5_background_mode is True


def test_only_verified_configured_process_window_is_hidden(tmp_path: Path) -> None:
    configured = tmp_path / "mt5" / "terminal64.exe"
    unrelated = tmp_path / "other" / "terminal64.exe"
    adapter = FakeWindowAdapter()
    adapter.identities = {11: _identity(11, configured), 22: _identity(22, unrelated)}
    adapter.windows = {11: (111,), 22: (222,)}
    adapter.owners = {111: 11, 222: 22}
    adapter.visible = {111: True, 222: True}

    controller = _controller(adapter, configured, lambda: (11, 22))
    assert controller.observe_once() == "HIDDEN"
    assert adapter.hidden == [111]
    assert adapter.visible == {111: False, 222: True}


def test_existing_verified_mt5_is_reused_and_hidden_without_launch(tmp_path: Path) -> None:
    configured = tmp_path / "terminal64.exe"
    configured.touch()
    adapter = FakeWindowAdapter()
    adapter.identities[77] = _identity(77, configured)
    adapter.windows[77] = (770,)
    adapter.owners[770] = 77
    adapter.visible[770] = True
    controller = _controller(adapter, configured, lambda: (77,))
    launches = 0

    def popen(*_args, **_kwargs):
        nonlocal launches
        launches += 1
        raise AssertionError("already-running MT5 must be reused")

    launcher = MT5AutoLauncher(
        Settings(mt5_terminal_path=str(configured), mt5_auto_launch=True,
                 mt5_background_mode=True),
        process_probe=lambda: (TerminalProcess(77),),
        popen_factory=popen,
        verifier=lambda *_args: MT5Verification(1, "demo", "XAUUSD", 2),
        background_window_controller=controller,
    )
    result = asyncio.run(launcher.ensure_ready(SimpleNamespace()))
    controller.stop()
    assert result.ready is True
    assert result.launch_state == "REUSED"
    assert launches == 0
    assert adapter.hidden == [770]


def test_delayed_window_creation_is_retried_with_a_bounded_wait(tmp_path: Path) -> None:
    configured = tmp_path / "terminal64.exe"
    adapter = FakeWindowAdapter()
    adapter.identities[31] = _identity(31, configured)
    adapter.owners[310] = 31
    adapter.visible[310] = True
    probes = 0

    def enumerate_window() -> None:
        nonlocal probes
        probes += 1
        if probes >= 3:
            adapter.windows[31] = (310,)

    adapter.on_enumerate = enumerate_window
    now = 0.0
    wait_calls = 0

    def fake_wait(seconds: float) -> bool:
        nonlocal now, wait_calls
        wait_calls += 1
        now += seconds
        return wait_calls >= 3  # stops the daemon monitor after startup succeeds

    controller = _controller(
        adapter,
        configured,
        lambda: (31,),
        retry_seconds=0.1,
        retry_interval=0.05,
        monotonic=lambda: now,
        wait=fake_wait,
    )
    controller.start(31)
    monitor_thread = controller._thread
    controller.stop()
    assert controller.state == "HIDDEN"
    assert probes >= 3
    assert adapter.hidden == [310]
    assert monitor_thread is not None
    assert not monitor_thread.is_alive()


def test_control_shutdown_stops_background_monitor(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{(tmp_path / 'control.db').as_posix()}")
    database.create_schema()
    stopped_monitors: list[bool] = []
    stopped_components: list[str] = []
    bootstrap = SimpleNamespace(
        stop_background_monitor=lambda: stopped_monitors.append(True),
    )
    supervisor = SimpleNamespace(
        stop_component=lambda component: stopped_components.append(component),
    )
    service = TelegramControlService(
        Settings(),
        tmp_path,
        supervisor=supervisor,
        database=database,
        mt5_bootstrap=bootstrap,
    )
    asyncio.run(service.stop())
    database.dispose()
    assert stopped_monitors == [True]
    assert stopped_components == ["live", "api"]


def test_window_timeout_does_not_kill_mt5_or_fail_readiness(tmp_path: Path) -> None:
    configured = tmp_path / "terminal64.exe"
    adapter = FakeWindowAdapter()
    adapter.identities[41] = _identity(41, configured)
    controller = _controller(adapter, configured, lambda: (41,))
    assert controller.observe_once() == "WAITING_FOR_WINDOW"
    assert adapter.identities[41].pid == 41  # fake process remains present
    launcher = MT5AutoLauncher(
        Settings(mt5_terminal_path=str(configured), mt5_background_mode=True),
        process_probe=lambda: (TerminalProcess(41),),
        verifier=lambda *_args: MT5Verification(1, "demo", "XAUUSD", 2),
        background_window_controller=controller,
    )
    result = asyncio.run(launcher.ensure_ready(SimpleNamespace()))
    controller.stop()
    assert result.ready is True
    assert result.checks["terminal"] == "CONNECTED"
    assert adapter.identities[41].pid == 41


def test_hide_failure_does_not_mark_mt5_disconnected(tmp_path: Path) -> None:
    configured = tmp_path / "terminal64.exe"
    adapter = FakeWindowAdapter()
    adapter.identities[51] = _identity(51, configured)
    adapter.windows[51] = (510,)
    adapter.owners[510] = 51
    adapter.visible[510] = True
    adapter.fail_hide = OSError("SECRET terminal account / MetaTrader window title")
    controller = _controller(adapter, configured, lambda: (51,))

    launcher = MT5AutoLauncher(
        Settings(mt5_terminal_path=str(configured), mt5_background_mode=True),
        process_probe=lambda: (TerminalProcess(51),),
        verifier=lambda *_args: MT5Verification(1, "demo", "XAUUSD", 2),
        background_window_controller=controller,
    )
    result = asyncio.run(launcher.ensure_ready(SimpleNamespace()))
    controller.stop()
    assert result.ready is True
    assert result.checks["terminal"] == "CONNECTED"
    assert controller.state == "ERROR"


def test_monitor_hides_a_new_verified_pid_after_restart(tmp_path: Path) -> None:
    configured = tmp_path / "terminal64.exe"
    adapter = FakeWindowAdapter()
    pids = [61]
    adapter.identities[61] = _identity(61, configured, 100)
    adapter.windows[61] = (610,)
    adapter.owners[610] = 61
    adapter.visible[610] = True
    controller = _controller(adapter, configured, lambda: tuple(pids))
    assert controller.observe_once() == "HIDDEN"

    pids[:] = [62]
    adapter.identities[62] = _identity(62, configured, 200)
    adapter.windows[62] = (620,)
    adapter.owners[620] = 62
    adapter.visible[620] = True
    assert controller.observe_once() == "HIDDEN"
    assert adapter.hidden == [610, 620]
    assert controller.status["pid"] == 62


def test_path_mismatch_and_pid_reuse_fail_closed(tmp_path: Path) -> None:
    configured = tmp_path / "configured" / "terminal64.exe"
    other = tmp_path / "other" / "terminal64.exe"
    adapter = FakeWindowAdapter()
    adapter.identities[71] = _identity(71, other)
    adapter.windows[71] = (710,)
    adapter.owners[710] = 71
    adapter.visible[710] = True
    controller = _controller(adapter, configured, lambda: (71,))
    assert controller.observe_once(preferred_pid=71) == "WAITING_FOR_PROCESS"
    assert adapter.hidden == []
    assert adapter.visible[710] is True

    adapter.identities[71] = _identity(71, configured, 1)
    adapter.windows[71] = (711,)
    adapter.owners[711] = 71
    adapter.visible[711] = True

    def reuse_pid() -> None:
        adapter.identities[71] = _identity(71, configured, 2)

    adapter.on_enumerate = reuse_pid
    assert controller.observe_once() == "ERROR"
    assert adapter.hidden == []


def test_unverifiable_candidate_makes_process_selection_fail_closed(tmp_path: Path) -> None:
    configured = tmp_path / "terminal64.exe"
    adapter = FakeWindowAdapter()
    adapter.identities[91] = _identity(91, configured)
    adapter.windows[91] = (910,)
    adapter.owners[910] = 91
    adapter.visible[910] = True
    controller = _controller(adapter, configured, lambda: (91, 92))
    assert controller.observe_once() == "ERROR"
    assert adapter.hidden == []
    assert adapter.visible[910] is True


def test_non_windows_mode_is_unavailable_and_never_calls_window_api(tmp_path: Path) -> None:
    controller = MT5BackgroundWindowController(
        enabled=True,
        configured_path=str(tmp_path / "terminal64.exe"),
        process_probe=lambda: (_ for _ in ()).throw(AssertionError("should not probe")),
        windows=False,
    )
    assert controller.observe_once() == "UNAVAILABLE"


def test_diagnostic_logs_never_include_exception_or_window_text(
    tmp_path: Path, caplog
) -> None:
    configured = tmp_path / "terminal64.exe"
    adapter = FakeWindowAdapter()
    adapter.identities[81] = _identity(81, configured)
    adapter.windows[81] = (810,)
    adapter.owners[810] = 81
    adapter.visible[810] = True
    adapter.fail_hide = RuntimeError("secret=do-not-log account=123 MetaTrader - Private")
    logger = logging.getLogger("phase36.window-test")
    controller = _controller(adapter, configured, lambda: (81,), logger=logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        assert controller.observe_once() == "ERROR"
    rendered = caplog.text
    assert "do-not-log" not in rendered
    assert "123" not in rendered
    assert "MetaTrader" not in rendered
    assert "RuntimeError" not in rendered  # status includes type only in structured state
    assert controller.status["last_error_type"] == "RuntimeError"
