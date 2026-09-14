"""Host restarts use operator approval; poweroff and disk destruction retain the floor."""

import pytest

from tools.approval import check_all_command_guards, check_dangerous_command, disable_session_yolo, enable_session_yolo
from tools.approval_context import reset_current_session_key, set_current_session_key


RESTARTS = [
    "reboot", "sudo /sbin/reboot", "systemctl reboot", "systemctl --force reboot",
    "shutdown -r now", "shutdown --reboot +1", "shutdown -fr now", "shutdown -f -r now",
    "init 6", "telinit 6", "true && (sudo reboot)", "{ reboot; }",
    'bash -c "shutdown -r now"', 'echo "$(grep x f; reboot)"',
]


@pytest.fixture
def approval_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    for name in ("HERMES_YOLO_MODE", "HERMES_GATEWAY_SESSION", "HERMES_CRON_SESSION", "HERMES_EXEC_ASK"):
        monkeypatch.delenv(name, raising=False)
    token = set_current_session_key(str(tmp_path))
    try:
        yield tmp_path
    finally:
        disable_session_yolo(str(tmp_path))
        reset_current_session_key(token)


@pytest.mark.parametrize("guard", [check_dangerous_command, check_all_command_guards])
@pytest.mark.parametrize("env_type", ["local", "ssh"])
@pytest.mark.parametrize("mode,choice,expected", [
    ("manual", "deny", False), ("manual", "once", True), ("off", "deny", True),
    ("yolo", "deny", True), ("cron-approve", "deny", True), ("cron-deny", "deny", False),
    ("user-deny", "once", False),
])
def test_restart_obeys_operator_approval(approval_home, monkeypatch, guard, env_type, mode, choice, expected):
    config_mode = "off" if mode in {"off", "user-deny"} else "manual"
    cron_mode = "approve" if mode == "cron-approve" else "deny"
    deny_rules = "  deny: ['*']\n" if mode == "user-deny" else ""
    (approval_home / "config.yaml").write_text(
        f"approvals:\n  mode: '{config_mode}'\n  cron_mode: {cron_mode}\n{deny_rules}"
        "security:\n  tirith_enabled: false\n"
    )
    if mode == "yolo":
        enable_session_yolo(str(approval_home))
    if mode.startswith("cron-"):
        monkeypatch.delenv("HERMES_INTERACTIVE")
        monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    for command in RESTARTS:
        calls = []
        result = guard(command, env_type, approval_callback=lambda *a, **kw: calls.append(a) or choice)
        assert result["approved"] is expected, (command, result)
        assert not result.get("hardline"), (command, result)
        assert bool(calls) is (mode == "manual"), command
    if mode == "manual":
        for command in ["echo reboot", "echo 'shutdown -r now'", "grep -F 'sudo reboot' notes.md"]:
            calls = []
            result = guard(command, env_type, approval_callback=lambda *a, **kw: calls.append(a) or "deny")
            assert result["approved"] is True and not calls, (command, result)


@pytest.mark.parametrize("command", [
    "poweroff", "halt", "shutdown -h now", "shutdown now", "init 0", "telinit 0",
    "systemctl poweroff", "shutdown -r -h now", "shutdown -hr now", "shutdown -r --poweroff now",
    "shutdown -r now --poweroff", "shutdown -r now -h",
    "reboot -p", "sudo /sbin/reboot --poweroff", "reboot --halt",
    "shutdown now 'maintenance -r'", "reboot; rm -rf /", "reboot; poweroff",
])
def test_restart_permission_preserves_other_hard_blocks(approval_home, command):
    (approval_home / "config.yaml").write_text("approvals:\n  mode: off\n")
    result = check_all_command_guards(command, "local")
    assert result["approved"] is False
    assert result.get("hardline") is True
