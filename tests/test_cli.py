import json
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

from local_ci import cli


def test_scan_main_checkouts(tmp_path: Path) -> None:
    for name in ("keep", "skip", "other"):
        (tmp_path / name / ".git").mkdir(parents=True)
    (tmp_path / "worktree").mkdir()
    (tmp_path / "worktree" / ".git").write_text("gitdir: elsewhere")
    assert [repo.name for repo in cli.scan_repos(tmp_path, ["k*", "s*"], ["skip"])] == ["keep"]


def test_launchd_print_and_install_without_launchctl(tmp_path: Path, monkeypatch, capfd) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "local-ci"))
    monkeypatch.setattr(cli.shutil, "which", lambda _: "/opt/homebrew/bin/local-ci")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("called"))
    )
    assert cli.main(["launchd", "install", "--print"]) == 0
    data = capfd.readouterr().out.encode()
    assert plistlib.loads(data)["StartInterval"] == 900
    assert not (tmp_path / "Library").exists()
    assert cli.main(["launchd", "install"]) == 0
    path = tmp_path / "Library" / "LaunchAgents" / "io.github.y-kanekoo.local-ci.plist"
    assert path.exists()
    assert plistlib.loads(path.read_bytes())["ProgramArguments"][-2:] == ["watch", "--once"]
    assert cli.main(["launchd", "uninstall"]) == 0
    assert not path.exists()


def test_watch_state_prevents_second_run(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (tmp_path / "config.yml").write_text(f"watch_repos: ['{repo}']\n")
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path))
    calls = []
    monkeypatch.setattr(cli, "run", lambda path, **kw: calls.append(kw) or {"status": "success"})
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            stdout=json.dumps([{"number": 3, "headRefOid": "abc", "headRefName": "feature"}])
        ),
    )
    assert cli.main(["watch", "--once"]) == 0
    assert cli.main(["watch", "--once"]) == 0
    assert calls == [{"pr": 3, "report": "none"}]
    assert list(json.loads((tmp_path / "state.json").read_text()).values()) == ["abc"]
