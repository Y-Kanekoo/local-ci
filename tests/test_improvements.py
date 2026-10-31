"""実リポジトリで見つかった実行上の問題の回帰テスト。"""

import json
import subprocess
from pathlib import Path

import pytest

from local_ci import cli, runner
from local_ci.config import Job, load_config


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def make_repo(tmp_path: Path, config: str, script: str = "print('ok')\n") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / ".local-ci.yml").write_text(config)
    (repo / "ci.py").write_text(script)
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "初期設定")
    return repo


def test_kind_order_across_monorepo_and_config_order(tmp_path: Path) -> None:
    for name in ("api", "frontend"):
        child = tmp_path / name
        child.mkdir()
        (child / "package.json").write_text(
            json.dumps({"scripts": {"e2e": "playwright test", "lint": "eslint ."}})
        )
    assert [job.kind for job in load_config(tmp_path).jobs] == [
        "setup",
        "setup",
        "lint",
        "lint",
        "e2e",
        "e2e",
    ]
    (tmp_path / ".local-ci.yml").write_text(
        "jobs:\n  browser:\n    kind: e2e\n    run: echo e2e\n"
        "  lint:\n    kind: lint\n    run: echo lint\n"
    )
    assert [job.name for job in load_config(tmp_path).jobs] == ["lint", "browser"]
    (tmp_path / ".local-ci.yml").write_text(
        "order: config\njobs:\n  browser:\n    kind: e2e\n    run: echo e2e\n"
        "  lint:\n    kind: lint\n    run: echo lint\n"
    )
    assert [job.name for job in load_config(tmp_path).jobs] == ["browser", "lint"]


def test_child_environment_removes_parent_tool_state(monkeypatch) -> None:
    for name in (
        "VIRTUAL_ENV_PROMPT",
        "PYTHONHOME",
        "PYTHONPATH",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "UV_PROJECT_ENVIRONMENT",
        "npm_config_registry",
        "npm_lifecycle_event",
        "npm_package_name",
    ):
        monkeypatch.setenv(name, "parent-value")
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/parent-venv")
    monkeypatch.setenv("PATH", "/tmp/parent-venv/bin:/usr/bin")
    monkeypatch.setenv("NODE_OPTIONS", "--trace-warnings")
    monkeypatch.setenv("GH_TOKEN", "ghp_abcdefgh123456")
    env = runner._child_env(Job("e2e", ["echo ok"], "e2e"))
    assert env["PATH"] == "/usr/bin"
    assert env["CI"] == "1"
    assert env["PLAYWRIGHT_FORCE_TTY"] == "0"
    assert env["NODE_OPTIONS"] == "--trace-warnings"
    for name in (
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PROMPT",
        "PYTHONHOME",
        "PYTHONPATH",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "UV_PROJECT_ENVIRONMENT",
        "npm_config_registry",
        "npm_lifecycle_event",
        "npm_package_name",
        "GH_TOKEN",
    ):
        assert name not in env


def test_progress_safe_log_allowed_failure_and_hint(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(
        tmp_path,
        "jobs:\n"
        "  frontend:lint:\n    kind: lint\n    run: python3 ci.py\n"
        "  e2e/browser:\n    kind: e2e\n    run: python3 fail.py\n"
        "    allow_failure: true\n",
    )
    (repo / "fail.py").write_text(
        "import os\nprint(os.environ['CI'], os.environ['PLAYWRIGHT_FORCE_TTY'])\n"
        "print('config.webServer')\nraise SystemExit(1)\n"
    )
    git(repo, "add", "fail.py")
    git(repo, "commit", "-qm", "失敗ケース")
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    result = runner.run(repo)
    assert result["status"] == "success"
    assert [job["name"] for job in result["jobs"]] == ["frontend:lint", "e2e/browser"]
    run_dir = Path(result["run_dir"])
    assert (run_dir / "frontend__lint.log").exists()
    assert "1 0" in (run_dir / "e2e__browser.log").read_text()
    assert "before フック" in (run_dir / "summary.md").read_text()
    assert "failed (allowed)" in (run_dir / "summary.md").read_text()
    assert "▶ frontend:lint" in capsys.readouterr().err
    runner.run(repo, quiet=True)
    assert capsys.readouterr().err == ""


def test_external_config_priority_and_plan_ref(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path, "jobs:\n  committed:\n    run: python3 ci.py\n    kind: test\n")
    home = tmp_path / "home"
    external = home / "repos" / "repo.yml"
    external.parent.mkdir(parents=True)
    external.write_text("jobs:\n  external:\n    run: python3 ci.py\n    kind: test\n")
    monkeypatch.setenv("LOCAL_CI_HOME", str(home))
    first_sha = git(repo, "rev-parse", "HEAD")
    (repo / ".local-ci.yml").unlink()
    git(repo, "add", "-u")
    git(repo, "commit", "-qm", "設定を削除")
    current = runner.plan(repo)
    assert current["config_source"] == str(external)
    assert [job["name"] for job in current["jobs"]] == ["external"]
    old = runner.plan(repo, first_sha)
    assert old["config_source"] == ".local-ci.yml"
    assert [job["name"] for job in old["jobs"]] == ["committed"]
    assert git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not list((home / "worktrees").iterdir())
    assert cli.main(["plan", str(repo), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["config_source"] == str(external)
    result = runner.run(repo, quiet=True)
    assert result["config_source"] == str(external)
    assert "config_source" in (Path(result["run_dir"]) / "summary.md").read_text()


def test_hooks_failure_cleanup_and_templates(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(
        tmp_path,
        "before:\n"
        "  - name: start\n    run: python3 fail.py {repo} {short_sha} {sha} {worktree}\n"
        "after:\n"
        "  - name: stop\n    run: python3 ci.py\n"
        "jobs:\n  test:\n    kind: test\n    run: python3 ci.py\n",
    )
    (repo / "fail.py").write_text(
        "import sys\nprint(' '.join(sys.argv[1:]))\nraise SystemExit(1)\n"
    )
    git(repo, "add", "fail.py")
    git(repo, "commit", "-qm", "フック")
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    result = runner.run(repo, quiet=True)
    assert result["status"] == "failure"
    assert [hook["status"] for hook in result["hooks"]] == ["failed", "success"]
    assert result["jobs"][0]["status"] == "skipped"
    assert result["jobs"][0]["skip_reason"].startswith("before")
    run_dir = Path(result["run_dir"])
    assert result["sha"] in (run_dir / "before__start.log").read_text()
    assert "ok" in (run_dir / "after__stop.log").read_text()
    assert len(json.loads((run_dir / "result.json").read_text())["hooks"]) == 2


def test_after_runs_on_interrupt(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(
        tmp_path,
        "after:\n  - name: stop\n    run: python3 ci.py\n"
        "jobs:\n  test:\n    kind: test\n    run: python3 ci.py\n",
    )
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    original = runner._run_job

    def interrupt(job, *args, **kwargs):
        if job.name == "test":
            raise KeyboardInterrupt
        return original(job, *args, **kwargs)

    monkeypatch.setattr(runner, "_run_job", interrupt)
    with pytest.raises(KeyboardInterrupt):
        runner.run(repo, quiet=True)
    results = list((tmp_path / "home" / "runs" / "repo").glob("*/result.json"))
    assert len(results) == 1
    stored = json.loads(results[0].read_text())
    assert stored["status"] == "interrupted"
    assert stored["hooks"][0]["name"] == "stop"
    assert not list((tmp_path / "home" / "worktrees").iterdir())


def test_plan_skip_reasons_and_after_failure_does_not_change_status(
    tmp_path: Path, monkeypatch
) -> None:
    repo = make_repo(
        tmp_path,
        "after:\n  - name: stop\n    run: python3 fail.py\n"
        "jobs:\n"
        "  skipped:\n    kind: lint\n    run: python3 ci.py\n    skip: true\n"
        "  service:\n    kind: e2e\n    run: python3 ci.py\n    needs_services: true\n"
        "  test:\n    kind: test\n    run: python3 ci.py\n",
    )
    (repo / "fail.py").write_text("raise SystemExit(1)\n")
    git(repo, "add", "fail.py")
    git(repo, "commit", "-qm", "停止失敗")
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    planned = runner.plan(repo)
    assert [(job["name"], job["skip"]) for job in planned["jobs"]] == [
        ("skipped", True),
        ("test", False),
        ("service", True),
    ]
    assert "設定" in planned["jobs"][0]["skip_reason"]
    assert "外部サービス" in planned["jobs"][2]["skip_reason"]
    result = runner.run(repo, quiet=True)
    assert result["status"] == "success"
    assert result["hooks"][0]["status"] == "failed"
    assert "before フック" not in (Path(result["run_dir"]) / "summary.md").read_text()


def test_run_all_progress_prefix(tmp_path: Path, monkeypatch, capsys) -> None:
    repo = make_repo(tmp_path, "jobs:\n  test:\n    kind: test\n    run: python3 ci.py\n")
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    assert cli.main(["run-all", "--root", str(tmp_path)]) == 0
    assert "[local-ci] repo: ▶ test" in capsys.readouterr().err
    assert repo.exists()
