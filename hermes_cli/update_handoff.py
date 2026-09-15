"""Durable verification handoff when a gateway restart kills its updater cgroup.

A detached session still belongs to systemd's service cgroup. The successor verifies
persisted runtime identities; an updater's exit code alone cannot discharge this work.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psutil

from utils import atomic_json_write

logger = logging.getLogger(__name__)
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

    home = get_process_hermes_home()
    identity = ur._code_identity(refresh=True)
    if ur._current is None or not identity.get("sha") or plan is None:
        raise RuntimeError("Cannot hand off update verification without receipt, target SHA and runtime inventory")
    handoff = {
        "schema": 1, "id": uuid4().hex, "phase": "awaiting_restart_verification",
        "owner_pid": os.getpid(), "owner_started": psutil.Process().create_time(),
        "expected": identity, "targets": plan.to_dict()["runtimes"],
        "update_complete": bool(update_complete), "receipt": copy.deepcopy(ur._current.data),
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
        rows = [row for row in fleet if row.get("profile") == profile]
        if not rows or any(row.get("code_sha") != expected or row.get("state") in {"down", "unknown"} for row in rows):
            errors.append(f"Gateway {profile!r} has not reported target commit {expected}")
    # Unexpected stale gateways are also a mixed fleet, even if absent from the initial plan.
    if any(row.get("code_sha") != expected for row in fleet):
        errors.append("The live fleet contains an unverified or different code generation")
    return errors


def _publish_completion(home: Path, handoff: dict) -> None:
    """Receipt first, obligation second, notification last; every step is restartable."""
    marker = home / "fleet_restart_pending"
    if marker.exists():
        pending = json.loads(marker.read_text(encoding="utf-8"))
        if pending.get("handoff_id") != handoff["id"]:
            raise RuntimeError("A newer update owns the fleet restart obligation")
    _persist_receipt(home, handoff)
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
