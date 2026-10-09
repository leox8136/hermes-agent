"""Frozen compat surface for releases that finish `hermes update` via the post-swap hand-off.

Releases from 2026-09-16 (94ced1a2b2) lazily import ``hermes_cli.update_handoff``
from the NEW tree after the checkout swap. Like every other retired updater
hook, this module keeps that import working by routing into the historical
takeover (``hermes_cli._old_updater`` → ``_update_takeover.py``) instead of
re-executing ``hermes update --post-swap``; the pulled tree is never imported
into the pre-pull interpreter.

Guarded by tests/compat/old_updater_surface.json — do not remove a public name
without regenerating the frozen surface and proving no shipped release loads it.
"""

from __future__ import annotations

import copy
import logging
import json
from contextlib import contextmanager
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4


logger = logging.getLogger(__name__)

# Set on the post-swap child: the receipt header says "continued", the lock is the parent's.
POST_SWAP_ENV = "HERMES_UPDATE_POST_SWAP"


def is_post_swap_child() -> bool:
    return os.environ.get(POST_SWAP_ENV) == "1"


def write_handoff(payload: dict[str, Any]) -> Path:
    """Persist the post-swap payload beside the receipts (the ROOT home); returns its path."""
    from hermes_constants import get_default_hermes_root

    directory = get_default_hermes_root() / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"post_swap_{os.getpid()}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def read_handoff(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"post-swap hand-off {path} is not a JSON object")
    return payload


def post_swap_python() -> Path:
    """Interpreter for the child: the project venv's python when this process runs from (or
    under) the project's Windows console shim — the shim can never be re-executed on Windows
    because it holds itself open for the whole process lifetime (#88838, #89599) — else the
    running interpreter."""
    if sys.platform != "win32":
        return Path(sys.executable)
    from hermes_cli._launchers import _is_windows
    from hermes_constants import project_venv_dir
    from pm.environments import venv_python

    venv_dir = project_venv_dir(Path(__file__).resolve().parents[1])
    if venv_dir is not None and _is_windows():
        candidate = venv_python(venv_dir, windows=True)
        if candidate.is_file():
            return candidate
    return Path(sys.executable)


def post_swap_command(handoff_path: Path, argv_tail: list[str]) -> list[str]:
    """``python -m hermes_cli.main update <original flags> --post-swap <file>``.

    Historical command shape; kept so a printed manual-continuation line reads
    exactly as older releases expect. The takeover child this tree actually
    spawns (see :func:`continue_update_in_fresh_interpreter`) ignores the flag.
    """
    return [str(post_swap_python()), "-m", "hermes_cli.main", "update", *argv_tail,
            "--post-swap", str(handoff_path)]


def post_swap_child_env() -> dict[str, str]:
    """Environment for the child. ``HERMES_UPDATE_REEXEC`` marks it as already off the Windows
    shim (no second re-exec at the sync boundary). The lock hand-off pid is only claimed when
    nobody upstream (Tauri/Electron updater) already named theirs."""
    from hermes_cli.update_lock import HANDOFF_PID_ENV

    env = {**os.environ, POST_SWAP_ENV: "1", "HERMES_UPDATE_REEXEC": "1"}
    env.setdefault(HANDOFF_PID_ENV, str(os.getpid()))
    return env


def _takeover_request(payload: dict[str, Any], argv_tail: list[str] | None) -> dict[str, Any]:
    """Map a historical post-swap payload onto the takeover request schema.

    Payloads written before the takeover seam (e.g. release 0.21.3) name the
    desktop flag and the Windows resume token differently and carry no
    assume_yes at all; `update_finish.py` reads the new names with bare
    subscriptions, so an unmapped payload KeyErrors mid-completion with the
    checkout already swapped. New-schema payloads pass through unchanged.
    """
    request = dict(payload)
    request.setdefault("desktop", bool(payload.get("had_desktop_app_before_update", False)))
    request.setdefault("windows_resume", payload.get("windows_gateway_resume"))
    request.setdefault("assume_yes", "--yes" in (argv_tail or []))
    request.setdefault("restart_update", False)
    return request


def _continue_legacy_post_swap(handoff_path: str | Path, *, argv_tail: list[str]) -> int:
    """Complete the command shape shipped before PM owned source updates.

    Those releases start the replacement checkout as ``hermes update <flags>
    --post-swap FILE``. The replacement bootstrap calls this before importing
    its PM or CLI graph, then the existing takeover child prepares and finishes
    the update entirely on replacement code.
    """
    payload = read_handoff(handoff_path)
    try:
        Path(handoff_path).unlink()
    except OSError:
        pass
    from hermes_cli._old_updater import _run_child

    code, _completed = _run_child(_takeover_request(payload, argv_tail))
    return int(code)


def continue_update_in_fresh_interpreter(payload: dict[str, Any], *, argv_tail: list[str] | None = None) -> int | None:
    """Run the post-swap tail in a child interpreter on the pulled code.

    The old caller's payload carries the same fields the completion request
    needs (receipt, plan, windows_resume, gateway_mode, ...); ``argv_tail`` is
    accepted and ignored because the takeover tail does not re-parse update
    flags. Returns the child's exit code, or ``None`` when no child could be
    started (the caller then owns the failure bookkeeping). Ctrl-C semantics
    mirror the historical hand-off: wait for the child's cleanup instead of
    subprocess.run's kill-on-interrupt.
    """
    from hermes_cli._old_updater import _run_child

    handoff_path = write_handoff(payload)
    request = _takeover_request(payload, argv_tail)
    cmd = post_swap_command(handoff_path, argv_tail or [])
    print(f"→ Post-swap hand-off: completing the update in a fresh interpreter ({handoff_path})")
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        code, _completed = _run_child(request)
        return int(code)
    except OSError as exc:
        print(f"  ⚠ Could not start the post-update interpreter: {exc}")
        print("  The code update is applied. Finish it with:")
        print(f"    {subprocess.list2cmdline(cmd)}")
        return None


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
    from utils import atomic_json_write

    atomic_json_write(path, data, mode=0o600, fsync_dir=True)


def _persist_receipt(home: Path, handoff: dict) -> None:
    directory = home / "logs" / "update_receipts"
    directory.mkdir(parents=True, exist_ok=True)
    from hermes_cli import update_receipt as ur

    receipt = handoff["receipt"]
    stored = ur.read_run_record(str(receipt.get("update_id")))
    archive = stored[0] if stored else ur._run_file(directory, receipt)
    _write(archive, receipt)
    _write(directory / "latest.json", handoff["receipt"])
    if handoff["phase"] == "completed":
        from hermes_cli.update_receipt import _prune_old_receipts
        _prune_old_receipts(directory)


def prepare_gateway_update_handoff(plan, *, update_complete: bool) -> None:
    """Checkpoint AFTER apply/maintenance and BEFORE any restart. Write failures abort restart."""
    from hermes_constants import get_process_hermes_home
    from hermes_cli import update_receipt as ur
    from hermes_cli.update_host_obligation import read_host_obligation
    import psutil

    home = get_process_hermes_home()
    identity = ur._code_identity(refresh=True)
    if ur._current.get() is None or not identity.get("sha") or plan is None:
        raise RuntimeError("Cannot hand off update verification without receipt, target SHA and runtime inventory")
    handoff = {
        "schema": 1, "id": uuid4().hex, "phase": "awaiting_restart_verification",
        "owner_pid": os.getpid(), "owner_started": psutil.Process().create_time(),
        "expected": identity, "targets": plan.to_dict()["runtimes"],
        "update_complete": bool(update_complete), "receipt": copy.deepcopy(ur._current.get().data),
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
    import psutil

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
    from utils import atomic_json_write

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


def defer_gateway_update_handoff(fleet: list[dict], *, update_complete: bool) -> bool:
    """Let the updater exit so its ancestor can restart; only the successor may finish."""
    from hermes_constants import get_process_hermes_home
    from hermes_cli import update_receipt as ur

    home = get_process_hermes_home()
    path = home / HANDOFF_NAME
    if not path.exists():
        return False
    with _handoff_lock(home) as acquired:
        if not acquired:
            raise RuntimeError("Update handoff is being settled by another process")
        handoff = json.loads(path.read_text(encoding="utf-8"))
        if handoff["owner_pid"] != os.getpid() or not _owner_alive(handoff):
            raise RuntimeError("Another updater owns the restart verification handoff")
        if ur._current.get() is None:
            raise RuntimeError("Cannot defer restart verification without its open receipt")
        handoff["receipt"] = copy.deepcopy(ur._current.get().data)
        handoff["receipt"]["fleet"] = fleet
        handoff["receipt"]["handoff"] = {"id": handoff["id"], "phase": handoff["phase"]}
        handoff["update_complete"] = bool(update_complete)
        _write(path, handoff)
        _persist_receipt(home, handoff)
        # The new completion protocol keeps a terminal code-update receipt;
        # gateway notification remains gated on this independent fleet handoff.
    return True


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
        if fleet_verified and not handoff["update_complete"]:
            handoff["receipt"].update(outcome="partial", exit_code=1)
        handoff["phase"] = "completed" if fleet_verified else "awaiting_restart_verification"
        handoff["fleet_verified"] = fleet_verified
        handoff["receipt"]["handoff"] = {"id": handoff["id"], "phase": handoff["phase"]}
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
