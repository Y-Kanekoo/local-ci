"""テスト標準の4段階（static・unit・api・e2e）のテスト。ネットワークは使わない。"""

import json
import subprocess
from pathlib import Path

import pytest

from local_ci import cli, notify
from local_ci.config import detect, load_config, parse_stages
from local_ci.runner import run


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def make_repo(root: Path, config: str, files: dict[str, str] | None = None) -> Path:
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".local-ci.yml").write_text(config)
    (repo / "ok.py").write_text("print('ok')\n")
    (repo / "ng.py").write_text("raise SystemExit(3)\n")
    for name, content in (files or {}).items():
        (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "initial")
    return repo


FOUR_STAGES = """
setup: python3 ok.py
jobs:
  e2e:
    kind: e2e
    run: python3 ok.py
  api:
    kind: api
    run: python3 ok.py
  unit:
    kind: test
    run: python3 ok.py
  lint:
    kind: lint
    run: python3 {lint}
  secrets:
    kind: lint
    stage: static
    run: python3 ok.py
"""


def test_stage_defaults_follow_kind_and_order_is_fixed(tmp_path: Path) -> None:
    (tmp_path / ".local-ci.yml").write_text(
        "jobs:\n"
        "  e2e:\n    run: echo e\n"
        "  contract:\n    kind: test\n    stage: api\n    run: echo c\n"
        "  build:\n    run: echo b\n"
        "  test:\n    run: echo t\n"
        "  typecheck:\n    run: echo y\n"
        "  lint:\n    run: echo l\n"
    )
    config = load_config(tmp_path)
    assert [(job.name, job.stage) for job in config.jobs] == [
        ("lint", "static"),
        ("typecheck", "static"),
        ("test", "unit"),
        ("build", "unit"),
        ("contract", "api"),
        ("e2e", "e2e"),
    ]
    assert config.fail_fast is False


def test_order_config_keeps_written_order(tmp_path: Path) -> None:
    (tmp_path / ".local-ci.yml").write_text(
        "order: config\njobs:\n  e2e:\n    run: echo e\n  lint:\n    run: echo l\n"
    )
    assert [job.name for job in load_config(tmp_path).jobs] == ["e2e", "lint"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("  t:\n    kind: test\n    stage: integration\n    run: echo x\n", "stage"),
        ("  s:\n    kind: setup\n    stage: static\n    run: echo x\n", "stage"),
        ("  t:\n    kind: test\n    skip_reason: 理由\n    run: echo x\n", "skip_reason"),
        (
            "  t:\n    kind: test\n    skip: true\n    skip_reason: ''\n    run: echo x\n",
            "skip_reason",
        ),
    ],
)
def test_invalid_stage_settings(tmp_path: Path, body: str, message: str) -> None:
    (tmp_path / ".local-ci.yml").write_text("jobs:\n" + body)
    with pytest.raises(ValueError, match=message):
        load_config(tmp_path)


def test_fail_fast_must_be_boolean(tmp_path: Path) -> None:
    (tmp_path / ".local-ci.yml").write_text(
        "fail_fast: yes-please\njobs:\n  test:\n    run: echo x\n"
    )
    with pytest.raises(ValueError, match="fail_fast"):
        load_config(tmp_path)


def test_parse_stages() -> None:
    assert parse_stages(None) is None
    assert parse_stages("static, unit") == {"static", "unit"}
    with pytest.raises(ValueError, match="--stage"):
        parse_stages("static,smoke")


def test_detect_assigns_stages_without_changing_existing_order(tmp_path: Path) -> None:
    scripts = {"lint": "eslint .", "test": "vitest", "build": "tsc", "e2e": "playwright test"}
    (tmp_path / "package.json").write_text(json.dumps({"scripts": scripts}))
    assert [(job.name, job.kind, job.stage) for job in load_config(tmp_path).jobs] == [
        ("install", "setup", "setup"),
        ("lint", "lint", "static"),
        ("test", "test", "unit"),
        ("build", "build", "unit"),
        ("e2e", "e2e", "e2e"),
    ]


def test_detect_node_api_scripts(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "test:api": "vitest run test/api"}})
    )
    by_name = {job.name: job for job in detect(tmp_path)}
    assert by_name["test"].run == ["npm run test"]
    assert (by_name["api"].run, by_name["api"].stage) == (["npm run test:api"], "api")
    split = tmp_path / "split"
    split.mkdir()
    (split / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "npm run test:unit && npm run test:api",
                    "test:unit": "vitest run test/unit",
                    "test:api": "vitest run test/api",
                }
            }
        )
    )
    by_name = {job.name: job for job in detect(split)}
    assert by_name["test"].run == ["npm run test:unit"]
    assert by_name["api"].run == ["npm run test:api"]


def test_detect_python_tests_api_directory(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="0.1"\n')
    assert [job.run for job in detect(tmp_path) if job.name == "test"] == [["pytest"]]
    (tmp_path / "tests" / "api").mkdir(parents=True)
    by_name = {job.name: job for job in detect(tmp_path)}
    assert by_name["test"].run == ["pytest --ignore=tests/api"]
    assert (by_name["api"].run, by_name["api"].stage) == (["pytest tests/api"], "api")


def test_run_stage_filter_runs_setup_and_selected_stage(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path, FOUR_STAGES.format(lint="ok.py"))
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    result = run(repo, stages={"unit", "api"}, quiet=True)
    assert [(job["name"], job["stage"]) for job in result["jobs"]] == [
        ("setup", "setup"),
        ("unit", "unit"),
        ("api", "api"),
    ]
    assert [entry["stage"] for entry in result["stages"]] == ["setup", "unit", "api"]
    with pytest.raises(ValueError, match="stage"):
        run(repo, stages={"smoke"}, quiet=True)


def test_fail_fast_skips_later_stages_with_reason(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path, "fail_fast: true\n" + FOUR_STAGES.format(lint="ng.py"))
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    result = run(repo, quiet=True)
    statuses = {job["name"]: job["status"] for job in result["jobs"]}
    assert result["status"] == "failure"
    assert statuses == {
        "setup": "success",
        "lint": "failed",
        "secrets": "success",  # 同じ static 段階の残りは実行する
        "unit": "skipped",
        "api": "skipped",
        "e2e": "skipped",
    }
    skipped = [job for job in result["jobs"] if job["status"] == "skipped"]
    assert all("前のステージ static が失敗" in job["skip_reason"] for job in skipped)
    stages = {entry["stage"]: entry for entry in result["stages"]}
    assert stages["static"]["status"] == "failure"
    assert stages["unit"]["status"] == "skipped"
    summary = (Path(result["run_dir"]) / "summary.md").read_text()
    assert "## ステージ別" in summary
    assert "| static | failure | 2 | 1 | 1 | 0 | - |" in summary


def test_without_fail_fast_all_stages_still_run(tmp_path: Path, monkeypatch) -> None:
    repo = make_repo(tmp_path, FOUR_STAGES.format(lint="ng.py"))
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    result = run(repo, quiet=True)
    assert result["fail_fast"] is False
    assert {job["name"]: job["status"] for job in result["jobs"]}["e2e"] == "success"
    forced = run(repo, quiet=True, fail_fast=True)
    assert {job["name"]: job["status"] for job in forced["jobs"]}["e2e"] == "skipped"


def test_skip_reason_is_recorded(tmp_path: Path, monkeypatch) -> None:
    config = (
        "jobs:\n  unit:\n    kind: test\n    run: python3 ok.py\n"
        "  e2e:\n    kind: e2e\n    skip: true\n    skip_reason: UI がないため（docs/TESTING.md）\n"
        "    run: npx playwright test\n"
    )
    repo = make_repo(tmp_path, config)
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    result = run(repo, quiet=True)
    assert result["status"] == "success"
    e2e = next(job for job in result["jobs"] if job["name"] == "e2e")
    assert e2e["skip_reason"] == "UI がないため（docs/TESTING.md）"
    stages = {entry["stage"]: entry for entry in result["stages"]}
    assert stages["e2e"] == {
        "stage": "e2e",
        "status": "skipped",
        "jobs": 1,
        "success": 0,
        "failed": 0,
        "skipped": 1,
        "skip_reasons": ["UI がないため（docs/TESTING.md）"],
    }
    assert "UI がないため" in (Path(result["run_dir"]) / "summary.md").read_text()


def test_aggregate_shows_stage_table() -> None:
    results = [
        {
            "repo": "app",
            "ref": "HEAD",
            "sha": "abcdef0123",
            "status": "failure",
            "jobs": [
                {"name": "lint", "kind": "lint", "stage": "static", "status": "success"},
                {"name": "unit", "kind": "test", "stage": "unit", "status": "failed"},
                {"name": "e2e", "kind": "e2e", "stage": "e2e", "status": "skipped"},
            ],
        },
        # stage を持たない旧形式の結果は kind から推定する
        {
            "repo": "old",
            "sha": "1",
            "status": "success",
            "jobs": [{"name": "test", "kind": "test", "status": "success"}],
        },
    ]
    summary = notify.aggregate(results)
    assert summary["repos"][0]["stages"] == {
        "static": "success",
        "unit": "failure",
        "e2e": "skipped",
    }
    assert summary["stage_counts"] == {
        "static": {"success": 1},
        "unit": {"failure": 1, "success": 1},
        "e2e": {"skipped": 1},
    }
    markdown = notify.render_markdown(summary)
    assert "| リポジトリ | static | unit | e2e |" in markdown
    assert "| app | ✅ success | ❌ failure | ⏭ skipped |" in markdown
    assert "| old | - | ✅ success | - |" in markdown


def test_cli_stage_option(tmp_path: Path, monkeypatch, capfd) -> None:
    repo = make_repo(tmp_path, FOUR_STAGES.format(lint="ok.py"))
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    assert cli.main(["run", str(repo), "--stage", "bogus", "--quiet"]) == 1
    assert "--stage" in capfd.readouterr().err
    assert cli.main(["run", str(repo), "--stage", "static", "--quiet"]) == 0
    capfd.readouterr()
    assert cli.main(["plan", str(repo), "--stage", "api", "--json"]) == 0
    planned = json.loads(capfd.readouterr().out)
    assert [job["name"] for job in planned["jobs"]] == ["setup", "api"]
    assert cli.main(["plan", str(repo)]) == 0
    text = capfd.readouterr().out
    assert "fail_fast: false" in text
    assert "secrets | lint | . | python3 ok.py | stage=static" in text


def test_four_stage_example_loads(tmp_path: Path) -> None:
    example = Path(__file__).parents[1] / "examples" / "four-stages.local-ci.yml"
    (tmp_path / ".local-ci.yml").write_text(example.read_text())
    config = load_config(tmp_path)
    assert config.fail_fast is True
    assert [(job.name, job.stage) for job in config.jobs] == [
        ("lint", "static"),
        ("secrets", "static"),  # 同じ stage 内は kind 順（lint → typecheck）の安定ソート
        ("typecheck", "static"),
        ("unit", "unit"),
        ("api", "api"),
        ("e2e", "e2e"),
    ]
    assert config.jobs[-1].skip_reason.startswith("UI がないため")
