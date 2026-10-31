import json
import subprocess
from pathlib import Path

from local_ci.runner import run


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".local-ci.yml").write_text("jobs:\n  test:\n    kind: test\n    run: python3 ci.py\n")
    (repo / "ci.py").write_text("import os\nprint(os.environ.get('CI'))\nprint('committed')\n")
    (repo / "file.txt").write_text("committed\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


def test_worktree_does_not_change_source(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    (repo / "file.txt").write_text("dirty\n")
    before = (_git(repo, "status", "--porcelain"), _git(repo, "rev-parse", "HEAD"))
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    result = run(repo)
    assert result["status"] == "success"
    assert (repo / "file.txt").read_text() == "dirty\n"
    assert before == (_git(repo, "status", "--porcelain"), _git(repo, "rev-parse", "HEAD"))
    assert _git(repo, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert not list((tmp_path / "home" / "worktrees").iterdir())
    run_dir = Path(result["run_dir"])
    stored = json.loads((run_dir / "result.json").read_text())
    assert stored["jobs"][0]["status"] == "success"
    assert "committed" in (run_dir / "test.log").read_text()
    assert "dirty" not in (run_dir / "test.log").read_text()
    assert (run_dir / "summary.md").exists()


def test_job_output_is_masked_and_gh_token_withheld(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    (repo / "ci.py").write_text(
        "import os\n"
        "print(os.environ.get('MY_SECRET'))\n"
        "print(os.environ.get('GH_TOKEN', 'not-passed'))\n"
    )
    _git(repo, "add", "ci.py")
    _git(repo, "commit", "-qm", "secret-test")
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("MY_SECRET", "secret-value-123")
    monkeypatch.setenv("GH_TOKEN", "ghp_abcdefgh123456")
    result = run(repo)
    run_dir = Path(result["run_dir"])
    for path in run_dir.iterdir():
        content = path.read_text()
        assert "secret-value-123" not in content
        assert "ghp_abcdefgh123456" not in content
    assert "not-passed" in (run_dir / "test.log").read_text()


def test_missing_tool_is_skipped(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    (repo / ".local-ci.yml").write_text(
        "jobs:\n  lint:\n    run: nonexistent-local-ci-tool\n    kind: lint\n"
    )
    _git(repo, "add", ".local-ci.yml")
    _git(repo, "commit", "-qm", "missing")
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    result = run(repo)
    assert result["status"] == "success"
    assert result["jobs"][0]["status"] == "skipped"
    assert "見つかりません" in (Path(result["run_dir"]) / "lint.log").read_text()


def test_timeout_kills_process_group(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    (repo / ".local-ci.yml").write_text("jobs:\n  test:\n    run: python3 ci.py\n    timeout: 1\n")
    (repo / "ci.py").write_text("import time\ntime.sleep(4)\n")
    _git(repo, "add", ".local-ci.yml", "ci.py")
    _git(repo, "commit", "-qm", "timeout")
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    result = run(repo)
    assert result["status"] == "failure"
    assert result["jobs"][0]["status"] == "timeout"


def test_only_still_runs_setup(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    (repo / ".local-ci.yml").write_text(
        "setup: python3 ci.py\n"
        "jobs:\n"
        "  lint:\n    kind: lint\n    run: python3 ci.py\n"
        "  test:\n    kind: test\n    run: python3 ci.py\n"
    )
    _git(repo, "add", ".local-ci.yml")
    _git(repo, "commit", "-qm", "setup")
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    result = run(repo, only={"lint"})
    assert [job["name"] for job in result["jobs"]] == ["setup", "lint"]
