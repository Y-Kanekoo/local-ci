"""compare（2つのコミットを同じ設定・段階ごとに比べる）のテスト。ネットワークは使わない。

K-20261101-08: 適用前のコミットに .local-ci.yml が無いと設定が変わり、
fail_fast で後段が skipped になって回帰が隠れる問題の再発防止。
"""

import json
import subprocess
from pathlib import Path

import pytest

from local_ci import cli
from local_ci.compare import build_comparison, classify, compare, render_markdown


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


CONFIG = """
fail_fast: true
jobs:
  lint:
    kind: lint
    run: python3 lint.py
  unit:
    kind: test
    run: python3 unit.py
"""

OK = "print('ok')\n"
NG = "raise SystemExit(3)\n"


def _commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, content in files.items():
        (repo / name).write_text(content)
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    return repo


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))


def test_base_without_config_uses_head_config_and_same_jobs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"lint.py": NG, "unit.py": OK}, "設定なし")
    head = _commit(repo, {"lint.py": OK, ".local-ci.yml": CONFIG}, "設定と修正")

    result = compare(repo, base, head, quiet=True)

    assert result["config"] == f"{head}:.local-ci.yml"
    assert [row["name"] for row in result["jobs"]] == ["lint", "unit"]
    assert {row["name"]: row["category"] for row in result["jobs"]} == {
        "lint": "fixed",
        "unit": "unchanged",
    }
    assert result["status"] == "ok"
    assert result["warnings"] == []


def test_fail_fast_is_disabled_so_later_stage_regression_is_visible(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"lint.py": OK, "unit.py": OK, ".local-ci.yml": CONFIG}, "base")
    head = _commit(repo, {"lint.py": NG, "unit.py": NG}, "壊す")

    result = compare(repo, base, head, quiet=True)

    rows = {row["name"]: row for row in result["jobs"]}
    assert rows["unit"]["head"] == "failed"
    assert rows["unit"]["category"] == "regression"
    assert rows["lint"]["category"] == "regression"
    assert result["status"] == "regression"
    static = next(entry for entry in result["stages"] if entry["stage"] == "static")
    unit = next(entry for entry in result["stages"] if entry["stage"] == "unit")
    assert (static["regression"], unit["regression"]) == (1, 1)


def test_still_failing_is_not_a_regression(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"lint.py": OK, "unit.py": NG, ".local-ci.yml": CONFIG}, "base")
    head = _commit(repo, {"README.md": "x\n"}, "無関係な変更")

    result = compare(repo, base, head, quiet=True)

    assert {row["name"]: row["category"] for row in result["jobs"]} == {
        "lint": "unchanged",
        "unit": "still_failing",
    }
    assert result["status"] == "ok"


def test_explicit_config_overrides_head_config(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"lint.py": OK, "unit.py": OK, ".local-ci.yml": CONFIG}, "base")
    head = _commit(repo, {"unit.py": NG}, "head")
    only_lint = tmp_path / "only-lint.yml"
    only_lint.write_text("jobs:\n  lint:\n    kind: lint\n    run: python3 lint.py\n")

    result = compare(repo, base, head, config_path=only_lint, quiet=True)

    assert [row["name"] for row in result["jobs"]] == ["lint"]
    assert result["status"] == "ok"


def test_external_config_is_used_when_head_has_none(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"lint.py": OK, "unit.py": OK}, "base")
    head = _commit(repo, {"unit.py": NG}, "head")
    external = tmp_path / "home" / "repos" / "repo.yml"
    external.parent.mkdir(parents=True)
    external.write_text(CONFIG)

    result = compare(repo, base, head, quiet=True)

    assert result["config"] == "LOCAL_CI_HOME/repos/repo.yml"
    assert {row["name"]: row["category"] for row in result["jobs"]} == {
        "lint": "unchanged",
        "unit": "regression",
    }


def test_stage_filter_limits_both_runs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"lint.py": OK, "unit.py": OK, ".local-ci.yml": CONFIG}, "base")
    head = _commit(repo, {"unit.py": NG}, "head")

    result = compare(repo, base, head, stages={"static"}, quiet=True)

    assert [row["name"] for row in result["jobs"]] == ["lint"]
    assert result["status"] == "ok"


@pytest.mark.parametrize(
    ("base", "head", "expected"),
    [
        ({"status": "success"}, {"status": "failed"}, "regression"),
        ({"status": "success"}, {"status": "timeout"}, "regression"),
        ({"status": "skipped"}, {"status": "failed"}, "regression"),
        ({"status": "failed"}, {"status": "success"}, "fixed"),
        ({"status": "failed"}, {"status": "failed"}, "still_failing"),
        ({"status": "success"}, {"status": "failed", "allow_failure": True}, "unchanged"),
        (None, {"status": "failed"}, "new_failure"),
        (None, {"status": "success"}, "unchanged"),
        ({"status": "success"}, None, "removed"),
    ],
)
def test_classify(base: dict | None, head: dict | None, expected: str) -> None:
    assert classify(base, head) == expected


def test_different_config_sources_produce_warning() -> None:
    base = {"repo": "r", "status": "success", "config_source": "auto", "jobs": []}
    head = {"repo": "r", "status": "success", "config_source": ".local-ci.yml", "jobs": []}

    result = build_comparison(base, head, "自動検出（各コミット）")

    assert len(result["warnings"]) == 1
    assert "設定の出所が違います" in result["warnings"][0]
    assert "警告" in render_markdown(result)


def test_cli_exit_code_and_json_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"lint.py": OK, "unit.py": OK, ".local-ci.yml": CONFIG}, "base")
    head = _commit(repo, {"unit.py": NG}, "head")
    output = tmp_path / "out" / "compare.json"

    code = cli.main(
        ["compare", str(repo), "--base", base, "--head", head, "--quiet", "--output", str(output)]
    )

    assert code == 1
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["status"] == "regression"
    assert saved["base"]["sha"] == base
    assert "回帰（base 成功 → head 失敗）" in capsys.readouterr().out
    assert cli.main(["compare", str(repo), "--base", base, "--head", base, "--quiet"]) == 0


def test_cli_missing_config_is_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, {"lint.py": OK, "unit.py": OK, ".local-ci.yml": CONFIG}, "base")

    code = cli.main(
        ["compare", str(repo), "--base", base, "--config", str(tmp_path / "none.yml"), "--quiet"]
    )

    assert code == 1
    assert "設定ファイルが見つかりません" in capsys.readouterr().err
