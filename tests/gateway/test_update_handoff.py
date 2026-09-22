"""An updater killed with its service cgroup leaves restartable verification, not success."""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from hermes_cli.update_handoff import HANDOFF_NAME

ROOT = Path(__file__).resolve().parents[2]

# Exercise the real post-pull boundary with expensive apply steps stubbed; handoff I/O,
# receipts, process identity, control sockets and gateway notification dispatch stay real.
UPDATER = '''
import os, time
from pathlib import Path
from types import SimpleNamespace
from hermes_cli import update_cmd as cmd, update_receipt as ur
from hermes_cli.update_inventory import UpdatePlan, RuntimeRecord
home = Path(os.environ["HERMES_HOME"])
ur.begin_update_receipt()
ur.record_step("dependency_sync", True)
plan = UpdatePlan(runtimes=[RuntimeRecord("gateway", "default", pid=os.getpid(), supervisor="systemd"),
                           RuntimeRecord("gateway", "work", pid=os.getpid(), supervisor="systemd")])
for name in ["_invalidate_update_cache", "_sweep_bytecode_after_update", "_sync_python_dependencies_after_pull"]:
    setattr(cmd, name, lambda *a, **k: None)
cmd._verify_head_after_pull = lambda *a, **k: ur._code_identity(refresh=True)["sha"]
cmd._update_node_dependencies = lambda: []
cmd._m()._build_web_ui = lambda *a: None
cmd._rebuild_desktop_after_update = lambda *a, **k: True
cmd._run_post_update_maintenance = lambda **k: True
# A SIGKILL here is the service-manager failure window: no finally/atexit can run.
def restart(*a):
    (home / "ready").write_text(str(os.getpid()))
    while True:
        time.sleep(1)
cmd._restart_gateway_fleet_after_update = restart
opts = SimpleNamespace(assume_yes=True, gw_input_fn=None, active_lazy_features=[],
                       active_tool_dependencies=[], pre_update_version="old", no_gateway_restart=False)
cmd._write_fleet_restart_pending_marker(expected_sha=ur._code_identity(refresh=True)["sha"],
    runtimes=plan.to_dict()["runtimes"])
cmd._finish_pulled_update(["git"], "current-ops", "old", opts,
    gateway_mode=True, is_fork=True, desktop_dir=home, had_desktop_app_before_update=False,
    pre_update_snapshot_id=None, _pre_update_plan=plan, _windows_gateway_resume=None)
'''

SOCKET = '''
import asyncio, json, os, sys
from pathlib import Path
from gateway.control_socket import GatewayControlServer
home = Path(sys.argv[1])
async def main():
    server = GatewayControlServer(home, verb_handlers={"identify": lambda: {
        "pid": os.getpid(), "served_profiles": json.loads(sys.argv[3]), "code_sha": sys.argv[2] or None, "code_version": "new", "supervisor": "systemd"}})
    assert await server.start()
    (home / "socket-ready").write_text("ready")
    try:
        await asyncio.Event().wait()
    finally:
        await server.stop()
asyncio.run(main())
'''


def _wait(path):
    deadline = time.monotonic() + 20
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"child never published {path}")
        time.sleep(0.05)


def _stop(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


async def _exercise(home, monkeypatch, launch, terminate, *, maintenance_ok=True, multiplex=False):
    import gateway.run as run
    import hermes_cli.update_handoff as handoff
    from gateway.config import Platform
    from gateway.run_notifications import GatewayNotificationsMixin

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home.parent)
    monkeypatch.setattr(run, "_hermes_home", home)
    home.mkdir()
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(home / "host-locks"))
    work = home / "profiles" / "work"
    work.mkdir(parents=True)
    (work / "config.yaml").write_text("{}\n")
    (home / ".update_pending.json").write_text(json.dumps({"platform": "telegram", "chat_id": "ops"}))
    runner = object.__new__(GatewayNotificationsMixin)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=True)))
    runner._authorization_adapter = lambda *a: adapter
    runner._pending_marker_metadata = lambda *a: {}
    proc = launch(home)
    sockets = []

    def gateway(profile_home, sha, served_profiles=None):
        (profile_home / "socket-ready").unlink(missing_ok=True)
        child = subprocess.Popen([sys.executable, "-c", SOCKET, str(profile_home), sha, json.dumps(served_profiles or [])], cwd=ROOT)
        sockets.append(child)
        _wait(profile_home / "socket-ready")
        return child

    try:
        _wait(home / "ready")
        state = json.loads((home / HANDOFF_NAME).read_text())
        expected = state["expected"]["sha"]
        assert state["receipt"]["steps"][0]["name"] == "dependency_sync"
        assert state["receipt"]["finished_at"] is None
        assert not (home / ".update_exit_code").exists()
        # Even a legacy wrapper's early zero must not bypass the durable obligation.
        (home / ".update_exit_code").write_text("0")
        assert not await runner._send_update_notification()
        adapter.send.assert_not_called()
        terminate(proc, home)
        default_gateway = gateway(home, expected)
        # An absent planned profile cannot be covered by the healthy default profile.
        assert not await runner._send_update_notification()
        assert (home / "fleet_restart_pending").exists()
        for sha in ("", "old-generation"):
            child = gateway(work, sha)
            assert not await runner._send_update_notification()
            adapter.send.assert_not_called()
            assert (home / "fleet_restart_pending").exists()
            _stop(child)
        if multiplex:
            _stop(default_gateway)
            gateway(home, expected, ["default", "work"])
        else:
            gateway(work, expected)
        # A successor must never settle a different update's host-wide obligation.
        from hermes_cli.update_host_obligation import read_host_obligation, amend_host_obligation
        captured = read_host_obligation()
        amend_host_obligation(expected_sha="newer-update")
        assert not await runner._send_update_notification()
        assert read_host_obligation()["expected_sha"] == "newer-update"
        adapter.send.assert_not_called()
        amend_host_obligation(expected_sha=captured["expected_sha"])
        # Crash after receipt + marker settlement but before notification IPC publication.
        write = handoff.atomic_json_write
        def fail_exit(path, *a, **kw):
            if Path(path).name == ".update_exit_code":
                raise OSError("simulated interrupted completion write")
            return write(path, *a, **kw)
        monkeypatch.setattr(handoff, "atomic_json_write", fail_exit)
        assert not await runner._send_update_notification()
        assert (home / HANDOFF_NAME).exists()
        adapter.send.assert_not_called()
        monkeypatch.setattr(handoff, "atomic_json_write", write)
        assert await runner._send_update_notification()
        adapter.send.assert_awaited_once()
        assert ("success" if maintenance_ok else "failed") in adapter.send.call_args.args[1]
        assert not (home / "fleet_restart_pending").exists()
        from hermes_cli.update_host_obligation import host_obligation_present
        assert not host_obligation_present()
        assert not (home / HANDOFF_NAME).exists()
        receipt = json.loads((home / "logs/update_receipts/latest.json").read_text())
        assert receipt["outcome"] == ("success" if maintenance_ok else "partial")
        assert receipt["finished_at"]
        assert receipt["post_update"]["sha"] == expected
        assert {row["profile"] for row in receipt["fleet"]} == ({"default"} if multiplex else {"default", "work"})
        assert receipt["gateway_restart"]["recovered_after_updater_exit"] is True
        assert not await runner._send_update_notification()
        adapter.send.assert_awaited_once()
    finally:
        terminate(proc, home)
        for child in sockets:
            _stop(child)


@pytest.mark.asyncio
@pytest.mark.parametrize("maintenance_ok", [True, False])
@pytest.mark.parametrize("multiplex", [True, False])
async def test_killed_updater_is_verified_by_successor_before_success(tmp_path, monkeypatch, maintenance_ok, multiplex):
    def launch(home):
        script = UPDATER.replace("_run_post_update_maintenance = lambda **k: True",
                                 f"_run_post_update_maintenance = lambda **k: {maintenance_ok}")
        return subprocess.Popen([sys.executable, "-c", script], cwd=ROOT, env={**os.environ, "HERMES_HOME": str(home)})
    await _exercise(tmp_path / ".hermes", monkeypatch, launch, lambda proc, home: _stop(proc),
                    maintenance_ok=maintenance_ok, multiplex=multiplex)


@pytest.mark.linux_only
@pytest.mark.live_system_guard_bypass
@pytest.mark.asyncio
async def test_systemd_mixed_kills_updater_but_successor_finishes_receipt(tmp_path, monkeypatch):
    import shutil
    if not shutil.which("systemd-run") or subprocess.run(
        ["systemctl", "--user", "show-environment"], capture_output=True
    ).returncode:
        pytest.skip("a user systemd manager is unavailable")
    unit = f"hermes-update-test-{uuid4().hex}.service"
    stopped = False
    def launch(home):
        script = home / "updater.py"
        script.write_text(UPDATER)
        parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,sys.argv[1]]); time.sleep(120)"
        subprocess.run(["systemd-run", "--user", "--quiet", f"--unit={unit}", "--property=KillMode=mixed",
                        "--property=TimeoutStopSec=1", f"--working-directory={ROOT}",
                        f"--setenv=HERMES_HOME={home}", f"--setenv=HERMES_GATEWAY_LOCK_DIR={home / 'host-locks'}", f"--setenv=PYTHONPATH={ROOT}",
                        sys.executable, "-c", parent, str(script)], check=True)
        return unit
    def terminate(unit, home):
        nonlocal stopped
        if stopped:
            return
        subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, timeout=15, check=True)
        stopped = True
        if (home / "ready").exists():
            from gateway.status import _pid_exists
            assert not _pid_exists(int((home / "ready").read_text()))
    await _exercise(tmp_path / ".hermes", monkeypatch, launch, terminate)
