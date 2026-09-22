"""Post-swap hand-off: finish ``hermes update`` in an interpreter born on the pulled code.

``hermes update`` starts in an interpreter that imported the PRE-pull tree. Once ``git merge``
(or the ZIP swap) has replaced the checkout, every later phase — dependency sync, Node/web/
Desktop builds, maintenance, fleet restart, verification, receipt — used to keep running in
that stale process and lazily import NEW source into an OLD ``sys.modules`` graph. Any rename
between the two commits then surfaced as an ``ImportError``/``AttributeError`` inside the
updater itself, after the code swap had already succeeded (#87134, #112465, #112558, #112604
and their siblings). Module purges and targeted reloads only ever moved the crash to the next
unpurged module.

The permanent shape: the pre-pull process stops at the swap, writes everything the tail needs
into a hand-off file and re-executes ``hermes update --post-swap <file>`` under the venv
interpreter. The child imports exclusively from the pulled tree, resumes the open receipt and
owns the rest of the run; the parent relays its exit code. Nothing in the updater runs pulled
code inside a pre-pull interpreter any more, so there is no stale-symbol class left to isolate.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil

from utils import atomic_json_write

from hermes_cli.update_cmd_common import _best_effort

logger = logging.getLogger("hermes_cli.update_cmd")

# Set on the post-swap child: the receipt header says "continued", the lock is the parent's.
POST_SWAP_ENV = "HERMES_UPDATE_POST_SWAP"
# Set on a child spawned DETACHED off the Windows console shim: the pid of the process that
# still holds ``hermes.exe`` open (the launcher, or the interpreter it ran). The child waits
# for it before it touches the venv —
# the shim quarantine is a single rename with sub-second retries, and the reporter's runs
# (#101600) reached it while the parent was still alive (relaunching gateways, or just
# tearing down), so the rename failed and the whole install was deferred.
SHIM_PARENT_PID_ENV = "HERMES_UPDATE_SHIM_PARENT_PID"
# Legacy re-exec (``_reexec_dependency_sync_off_windows_shim``, no hand-off file): the Windows
# pause token travels here so the child resumes exactly the fleet the parent stopped instead
# of re-running pause discovery — which found the parent's freshly relaunched gateway before
# its pid file existed and force-killed it as "unmapped" (#101600).
GATEWAY_RESUME_ENV = "HERMES_UPDATE_GATEWAY_RESUME"

SHIM_PARENT_EXIT_TIMEOUT_SECONDS = 30.0


def _json_default(value: Any):
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def write_handoff(payload: dict[str, Any]) -> Path:
    """Persist the post-swap payload under HERMES_HOME; returns its path."""
    from hermes_constants import get_hermes_home

    directory = get_hermes_home() / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"post_swap_{os.getpid()}.json"
    path.write_text(json.dumps(payload, indent=2, default=_json_default), encoding="utf-8")
    return path


def read_handoff(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"post-swap hand-off {path} is not a JSON object")
    return payload


def is_post_swap_child() -> bool:
    return os.environ.get(POST_SWAP_ENV) == "1"


def _running_from_windows_shim() -> bool:
    from hermes_cli.main_install_repair import _windows_shim_in_process_chain

    return _windows_shim_in_process_chain() is not None


def post_swap_python() -> Path:
    """Interpreter for the child: the project venv's python (the console shim can never be
    re-executed on Windows — it holds itself open), else the running interpreter."""
    from hermes_cli.main_install_repair import _windows_shim_in_process_chain

    shim = _windows_shim_in_process_chain()
    if shim is not None:
        from hermes_constants import venv_python_path

        candidate = venv_python_path(shim.parent.parent, windows=True)
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def post_swap_command(handoff_path: Path, argv_tail: list[str]) -> list[str]:
    """``python -m hermes_cli.main update <original flags> --post-swap <file>``."""
    return [str(post_swap_python()), "-m", "hermes_cli.main", "update", *argv_tail, "--post-swap", str(handoff_path)]


def post_swap_child_env() -> dict[str, str]:
    """Environment for the child. ``HERMES_UPDATE_REEXEC`` marks it as already off the Windows
    shim (no second re-exec at the sync boundary; ``cmd_update`` hard-exits it when its receipt
    is durable instead of waiting on a leftover non-daemon thread). The lock hand-off pid is
    only claimed when nobody upstream (Tauri/Electron updater) already named theirs."""
    from hermes_cli.main_install_repair import _UPDATE_REEXEC_ENV
    from hermes_cli.update_lock import HANDOFF_PID_ENV

    env = {**os.environ, POST_SWAP_ENV: "1", _UPDATE_REEXEC_ENV: "1"}
    env.setdefault(HANDOFF_PID_ENV, str(os.getpid()))
    return env


def detached_shim_child_env(env: dict[str, str], gateway_resume: dict | None = None) -> dict[str, str]:
    """Env for a child that outlives this shim-run process: names the process holding the shim
    open (the ``hermes.exe`` launcher above us when psutil sees it, else this interpreter) and,
    for the legacy re-exec (no hand-off file), carries the Windows pause token."""
    from hermes_cli.main_install_repair import _windows_shim_holder_pid

    env = {**env, SHIM_PARENT_PID_ENV: str(_windows_shim_holder_pid())}
    if gateway_resume is not None:
        env[GATEWAY_RESUME_ENV] = json.dumps(gateway_resume, default=_json_default)
    return env


def adopt_handed_off_gateway_resume() -> dict | None:
    """The pause token a shim-run parent handed to this legacy re-exec child, or ``None``.
    Consumed on read so nothing this run spawns (relaunched gateways) inherits it."""
    raw = os.environ.pop(GATEWAY_RESUME_ENV, None)
    if not raw:
        return None
    try:
        token = json.loads(raw)
    except ValueError:
        logger.warning("Ignoring malformed %s from the update hand-off", GATEWAY_RESUME_ENV)
        return None
    return token if isinstance(token, dict) else None


def wait_for_shim_parent_exit(timeout: float = SHIM_PARENT_EXIT_TIMEOUT_SECONDS) -> bool:
    """Block until the shim-run parent named in :data:`SHIM_PARENT_PID_ENV` has exited.

    Returns True when it is gone (or none was named), False when it outlived ``timeout``; the
    caller proceeds either way — the strict shim quarantine refuses if it still holds the exe.
    A pid younger than this process is a recycled pid, never our parent. Consumed on read.
    """
    raw = os.environ.pop(SHIM_PARENT_PID_ENV, "")
    try:
        pid = int(raw.strip())
    except ValueError:
        return True
    if pid <= 0 or pid == os.getpid():
        return True
    import psutil

    try:
        parent = psutil.Process(pid)  # pins (pid, create_time): a recycled pid reads as gone
        if parent.create_time() > psutil.Process().create_time():
            return True
        deadline = time.monotonic() + timeout
        while parent.is_running() and parent.status() != psutil.STATUS_ZOMBIE:
            if time.monotonic() >= deadline:
                print(f"  ⚠ The hermes.exe process that started this update (PID {pid}) is still "
                      f"running after {int(timeout)}s; continuing.")
                return False
            time.sleep(0.2)
    except psutil.Error:
        pass
    return True


def _print_manual_continuation(cmd: list[str], exc: OSError) -> None:
    logger.warning("Post-swap hand-off could not start: %s", exc)
    print(f"  ⚠ Could not start the post-update interpreter: {exc}")
    print("  The code update is applied. Finish it with:")
    print(f"    {subprocess.list2cmdline(cmd)}")


def continue_update_in_fresh_interpreter(payload: dict[str, Any], *, argv_tail: list[str]) -> int | None:
    """Run the post-swap tail in a child interpreter on the pulled code.

    Returns the child's exit code, or ``None`` when no child could be started (the caller then
    owns the failure bookkeeping). The parent has already detached from the receipt (the child
    resumes it) and only relays the exit code. On Windows, when this process runs from
    ``hermes.exe``, the child cannot be awaited: the shim is one of the files the dependency
    sync must replace and it stays open for as long as this process lives (#88838, #89599).
    That case spawns detached, prints where the run continues and returns 0 — the child prints
    its own result and ``--gateway`` writes the true exit code to ``.update_exit_code``.

    Ctrl-C reaches parent and child together; the child owns the receipt and the Windows
    gateway resume, so the parent keeps waiting for it instead of ``subprocess.run``'s
    kill-on-interrupt, which would cut it off mid-cleanup.
    """
    handoff_path = write_handoff(payload)
    cmd = post_swap_command(handoff_path, argv_tail)
    env = post_swap_child_env()
    logger.debug("Post-swap hand-off → %s", subprocess.list2cmdline(cmd))
    sys.stdout.flush()
    sys.stderr.flush()

    if _running_from_windows_shim():
        try:
            subprocess.Popen(cmd, env=detached_shim_child_env(env), stdin=subprocess.DEVNULL)
        except OSError as exc:
            _print_manual_continuation(cmd, exc)
            return None
        print("→ Windows: hermes.exe cannot replace itself while it runs; the update")
        print("  continues under the venv Python. The code update is already applied and")
        print("  this shell returns right away; the install finishes below.")
        return 0

    try:
        child = subprocess.Popen(cmd, env=env, stdin=sys.stdin)
    except OSError as exc:
        _print_manual_continuation(cmd, exc)
        return None
    try:
        return int(child.wait())
    except KeyboardInterrupt:
        print("\n  Interrupted — waiting for the update child to finish its cleanup...")
        try:
            return int(child.wait(timeout=60))
        except subprocess.TimeoutExpired:
            child.terminate()
            return 130
        except KeyboardInterrupt:
            child.terminate()
            return 130


# Gateway restart recovery also lives here: older running updaters import these
# entry points after pulling new source, before a post-swap interpreter exists.
HANDOFF_NAME = ".update_handoff.json"


@contextmanager
def _handoff_lock(home: Path):
    from gateway.status import _release_file_lock, _try_acquire_file_lock

    with (home / ".update_handoff.lock").open("a+") as handle:
        acquired = _try_acquire_file_lock(handle)
        try:
            yield acquired
        finally:
            if acquired:
                _release_file_lock(handle)


def _write(path: Path, data: dict) -> None:
    atomic_json_write(path, data, mode=0o600, fsync_dir=True)


def _persist_receipt(home: Path, handoff: dict) -> None:
    directory = home / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    _write(directory / f"update_{handoff['id']}.json", handoff["receipt"])
    _write(directory / "latest.json", handoff["receipt"])
    if handoff["phase"] == "completed":
        from hermes_cli.update_receipt import _prune_old_receipts
        _prune_old_receipts(directory)


def prepare_gateway_update_handoff(plan, *, update_complete: bool) -> None:
    """Checkpoint AFTER apply/maintenance and BEFORE any restart. Write failures abort restart."""
    from hermes_constants import get_process_hermes_home
    from hermes_cli import update_receipt as ur
    from hermes_cli.update_host_obligation import read_host_obligation

    home = get_process_hermes_home()
    identity = ur._code_identity(refresh=True)
    if ur._current is None or not identity.get("sha") or plan is None:
        raise RuntimeError("Cannot hand off update verification without receipt, target SHA and runtime inventory")
    handoff = {
        "schema": 1, "id": uuid4().hex, "phase": "awaiting_restart_verification",
        "owner_pid": os.getpid(), "owner_started": psutil.Process().create_time(),
        "expected": identity, "targets": plan.to_dict()["runtimes"],
        "update_complete": bool(update_complete), "receipt": copy.deepcopy(ur._current.data),
        "host_obligation": read_host_obligation(),
    }
    handoff["receipt"]["handoff"] = {"id": handoff["id"], "phase": handoff["phase"]}
    with _handoff_lock(home) as acquired:
        if not acquired:
            raise RuntimeError("Another process is settling the update verification handoff")
        # The marker binds the obligation to THIS attempt, not an older latest.json.
        _write(home / "fleet_restart_pending", {"handoff_id": handoff["id"], "expected_sha": identity["sha"]})
        _persist_receipt(home, handoff)
        _write(home / HANDOFF_NAME, handoff)
        (home / ".update_exit_code").unlink(missing_ok=True)


def _owner_alive(handoff: dict) -> bool:
    try:
        proc = psutil.Process(handoff["owner_pid"])
        return proc.create_time() == handoff["owner_started"] and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        return True


def _verification_errors(handoff: dict, fleet: list[dict]) -> list[str]:
    expected = handoff["expected"]["sha"]
    errors = []
    targets = handoff["targets"]
    if not targets:
        errors.append("No gateway identities were captured before restart")
    for target in targets:
        kind, profile = target.get("kind"), target.get("profile")
        if kind != "gateway" or not profile or profile == "unknown":
            errors.append(f"Cannot verify {kind} runtime {profile!r} through the gateway identity probe")
            continue
        rows = [row for row in fleet if row.get("profile") == profile or profile in row.get("served_profiles", [])]
        if not rows or any(row.get("code_sha") != expected or row.get("state") in {"down", "unknown"} for row in rows):
            errors.append(f"Gateway {profile!r} has not reported target commit {expected}")
    # Unexpected stale gateways are also a mixed fleet, even if absent from the initial plan.
    if any(row.get("code_sha") != expected for row in fleet):
        errors.append("The live fleet contains an unverified or different code generation")
    return errors


def _publish_completion(home: Path, handoff: dict) -> None:
    """Receipt first, obligation second, notification last; every step is restartable."""
    marker = home / "fleet_restart_pending"
    from hermes_cli.update_host_obligation import host_obligation_present, read_host_obligation, host_obligation_path

    host_pending = host_obligation_present()
    if host_pending:
        current = read_host_obligation()
        captured = handoff.get("host_obligation")
        if not current or not captured or any(
            current.get(key) != captured.get(key) for key in ("started", "pid", "expected_sha", "inventory")
        ):
            raise RuntimeError("A different update owns the host restart obligation")
    if marker.exists():
        pending = json.loads(marker.read_text(encoding="utf-8"))
        if pending.get("handoff_id") != handoff["id"]:
            raise RuntimeError("A newer update owns the fleet restart obligation")
    _persist_receipt(home, handoff)
    if host_pending and handoff["fleet_verified"]:
        host_obligation_path().unlink(missing_ok=True)
    if marker.exists() and handoff["fleet_verified"]:
        marker.unlink()
    # JSON numbers are also valid for the existing plain-integer IPC reader.
    atomic_json_write(home / ".update_exit_code", 0 if handoff["receipt"]["outcome"] == "success" else 1,
                      fsync_dir=True)
    (home / HANDOFF_NAME).unlink()


def resume_gateway_update_handoff(home: Path) -> bool:
    """One read-only fleet probe, then durable settlement. False means do NOT report completion.

    Called at gateway startup and by the existing update watcher. Never restarts anything.
    The old updater must be dead; PID reuse cannot transfer its ownership to an unrelated process.
    """
    path = home / HANDOFF_NAME
    if not path.exists():
        return True
    try:
        with _handoff_lock(home) as acquired:
            if not acquired:
                return False
            if not path.exists():
                return True
            handoff = json.loads(path.read_text(encoding="utf-8"))
            if handoff.get("schema") != 1 or _owner_alive(handoff):
                return False
            if handoff["phase"] == "completed":
                _publish_completion(home, handoff)
                return True
            marker = home / "fleet_restart_pending"
            if not marker.exists() or json.loads(marker.read_text(encoding="utf-8")).get("handoff_id") != handoff["id"]:
                return False
            from hermes_cli.update_receipt import collect_fleet_versions, _utc_now_iso

            fleet = collect_fleet_versions(pre_restart_pids=[target["pid"] for target in handoff["targets"] if target.get("pid")])
            errors = _verification_errors(handoff, fleet)
            receipt = handoff["receipt"]
            receipt["fleet"] = fleet
            receipt["gateway_restart"] = {"incomplete": bool(errors), "verification_errors": errors,
                                          "recovered_after_updater_exit": True}
            if errors:
                # Keep both the obligation and the pending notification; a later poll can observe
                # another profile that is still starting. No success inferred from missing rows.
                _persist_receipt(home, handoff)
                return False
            receipt.update({"outcome": "success" if handoff["update_complete"] else "partial",
                            "exit_code": 0 if handoff["update_complete"] else 1,
                            "finished_at": _utc_now_iso(), "post_update": handoff["expected"],
                            "stop_reason": "verified by successor gateway"})
            handoff["phase"] = "completed"
            handoff["fleet_verified"] = True
            receipt["handoff"]["phase"] = "completed"
            _write(path, handoff)
            _publish_completion(home, handoff)
            return True
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        logger.exception("Update handoff verification remains pending")
        return False


def checkpoint_verified_handoff(receipt_path: Path | None, *, fleet_verified: bool) -> None:
    """Commit the original updater's final receipt before it clears the restart marker."""
    from hermes_constants import get_process_hermes_home

    home = get_process_hermes_home()
    path = home / HANDOFF_NAME
    if not path.exists():
        return
    if receipt_path is None:
        raise RuntimeError("Cannot settle update handoff without a durable final receipt")
    with _handoff_lock(home) as acquired:
        if not acquired:
            raise RuntimeError("Update handoff is being settled by another process")
        handoff = json.loads(path.read_text(encoding="utf-8"))
        if handoff["owner_pid"] != os.getpid() or not _owner_alive(handoff):
            return
        handoff["receipt"] = json.loads(receipt_path.read_text(encoding="utf-8"))
        handoff["phase"] = "completed"
        handoff["fleet_verified"] = fleet_verified
        handoff["receipt"]["handoff"] = {"id": handoff["id"], "phase": "completed"}
        _write(path, handoff)


def gateway_handoff_fleet_errors(fleet: list[dict]) -> list[str]:
    """Apply the same identity coverage to the original updater's successful return path.

    Serve/dashboard verification is owned by the normal updater's separate runtime probe.
    A successor cannot repeat that probe's restart bookkeeping and remains conservative.
    """
    from hermes_constants import get_process_hermes_home

    path = get_process_hermes_home() / HANDOFF_NAME
    if not path.exists():
        return []
    handoff = json.loads(path.read_text(encoding="utf-8"))
    if handoff["owner_pid"] != os.getpid() or not _owner_alive(handoff):
        return []
    handoff["targets"] = [target for target in handoff["targets"] if target.get("kind") == "gateway"]
    return _verification_errors(handoff, fleet)


def retire_gateway_update_handoff() -> None:
    """The original updater completed verification itself; its normal receipt owns the outcome."""
    from hermes_constants import get_process_hermes_home

    home = get_process_hermes_home()
    path = home / HANDOFF_NAME
    if path.exists():
        with _handoff_lock(home) as acquired:
            if acquired:
                handoff = json.loads(path.read_text(encoding="utf-8"))
                if handoff["owner_pid"] == os.getpid() and _owner_alive(handoff) and handoff["phase"] == "completed":
                    _publish_completion(home, handoff)
