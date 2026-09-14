"""Default checks follow the operations fork, even when an upstream remote exists."""

import argparse
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli.subcommands.update import build_update_parser
from hermes_cli.update_target import DEFAULT_UPDATE_BRANCH, DEFAULT_UPDATE_REPO


@pytest.mark.parametrize("branch", [None, " ", "release-test"])
def test_default_check_fetches_origin_release_channel(tmp_path, monkeypatch, capsys, branch):
    from hermes_cli import main
    from hermes_cli.main_install_repair import _resolve_update_branch

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"

    def git(*args, cwd=source):
        return subprocess.check_output(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args],
            cwd=cwd, text=True, stderr=subprocess.PIPE,
        ).strip()

    source.mkdir()
    git("init", "-b", DEFAULT_UPDATE_BRANCH)
    git("commit", "--allow-empty", "-m", "base")
    git("branch", "release-test")
    git("branch", "main")
    git("clone", "--no-local", "--branch", DEFAULT_UPDATE_BRANCH, str(source), str(checkout), cwd=tmp_path)
    git("remote", "add", "upstream", str(source), cwd=checkout)
    args = SimpleNamespace(check=True, branch=branch)
    target = _resolve_update_branch(args)
    git("checkout", target)
    git("commit", "--allow-empty", "-m", "release update")
    expected_sha = git("rev-parse", "HEAD")
    monkeypatch.setattr(main, "PROJECT_ROOT", checkout)

    assert main._update_preflight_handled(args) is True
    assert git("rev-parse", f"origin/{target}", cwd=checkout) == expected_sha
    assert git("branch", "--show-current", cwd=checkout) == DEFAULT_UPDATE_BRANCH
    assert not git("for-each-ref", "refs/remotes/upstream", cwd=checkout)
    assert f"origin/{target}" in capsys.readouterr().out


def test_parser_and_zip_fallback_share_fork_release_channel(tmp_path, monkeypatch):
    from hermes_cli import main, update_cmd_zip
    from hermes_cli.main_install_repair import _resolve_update_branch

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    parser = argparse.ArgumentParser()
    build_update_parser(parser.add_subparsers(), cmd_update=lambda args: None)
    args = parser.parse_args(["update"])
    assert _resolve_update_branch(args) == _resolve_update_branch(SimpleNamespace())
    explicit = parser.parse_args(["update", "--branch", "release-test"])
    assert _resolve_update_branch(explicit) == "release-test"

    monkeypatch.setattr(main, "_capture_active_tool_dependencies", lambda: {})
    monkeypatch.setattr(update_cmd_zip, "_abort_zip_update_if_dirty_tree", lambda: None)
    requests = []

    class DownloadReached(Exception):
        pass

    def download(branch, url):
        requests.append((branch, url))
        raise DownloadReached

    monkeypatch.setattr(update_cmd_zip, "_download_and_swap_zip", download)
    with pytest.raises(DownloadReached):
        update_cmd_zip._update_via_zip(args)
    assert requests == [(
        _resolve_update_branch(args),
        f"https://github.com/{DEFAULT_UPDATE_REPO}/archive/refs/heads/{_resolve_update_branch(args)}.zip",
    )]
