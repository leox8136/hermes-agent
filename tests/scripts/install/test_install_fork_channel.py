"""Run the installer repository stage against local Git transport, without installing dependencies."""

import os
import subprocess
from pathlib import Path

import pytest

from hermes_cli.update_target import DEFAULT_UPDATE_BRANCH, DEFAULT_UPDATE_REPO

ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("branch", [None, "release-test"])
def test_repository_stage_clones_fork_channel_and_preserves_explicit_branch(tmp_path, branch):
    upstream = tmp_path / "repo"
    config = tmp_path / "gitconfig"
    def git(*args, cwd=None):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
    git("init", "-b", DEFAULT_UPDATE_BRANCH, str(upstream))
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-m", "base", cwd=upstream)
    git("branch", "release-test", cwd=upstream)
    for url in [f"git@github.com:{DEFAULT_UPDATE_REPO}.git", f"https://github.com/{DEFAULT_UPDATE_REPO}.git"]:
        git("config", "--file", str(config), "--add", f"url.{upstream.as_uri()}.insteadOf", url)
    home = tmp_path / "home"
    home.mkdir()
    destination = tmp_path / "installed"
    env = {**os.environ, "HOME": str(home), "HERMES_HOME": str(home / ".hermes"),
           "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1",
           # Any accidental official URL fails immediately instead of accessing the network.
           "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"}
    args = ["bash", str(ROOT / "scripts/install.sh"), "--stage", "repository", "--dir", str(destination), "--json"]
    if branch:
        args += ["--branch", branch]
    result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert git("branch", "--show-current", cwd=destination) == (branch or DEFAULT_UPDATE_BRANCH)
    assert DEFAULT_UPDATE_REPO in git("config", "--get", "remote.origin.url", cwd=destination)
