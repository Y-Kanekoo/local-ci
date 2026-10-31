"""集計出力と Discord 送信のテスト（ネットワークは使わない）。"""

import json
import urllib.error
from pathlib import Path

import pytest

from local_ci import cli, notify, runner
from local_ci.config import load_config

WEBHOOK = "https://discord.com/api/webhooks/123456/abcdefSECRETtoken"


def result(repo: str, status: str, jobs: list[tuple[str, str, bool]]) -> dict[str, object]:
    return {
        "repo": repo,
        "ref": "HEAD",
        "sha": "0123456789abcdef",
        "status": status,
        "hooks": [],
        "jobs": [
            {"name": name, "kind": "test", "status": state, "allow_failure": allowed}
            for name, state, allowed in jobs
        ],
        "run_dir": f"/tmp/{repo}",
    }


def sample() -> list[dict[str, object]]:
    return [
        result("b-app", "failure", [("lint", "success", False), ("test", "failed", False)]),
        result("a-app", "success", [("e2e", "failed", True), ("test", "skipped", False)]),
        {"repo": "c-app", "status": "error", "error": "not a git repo", "jobs": []},
    ]


def test_aggregate_and_render() -> None:
    summary = notify.aggregate(sample())
    assert summary["status"] == "failure"
    assert [repo["repo"] for repo in summary["repos"]] == ["a-app", "b-app", "c-app"]
    assert summary["repos"][1]["failed_jobs"] == ["test"]
    assert summary["repos"][0]["failed_jobs"] == []
    assert summary["job_counts"] == {
        "failed": 1,
        "failed (allowed)": 1,
        "skipped": 1,
        "success": 1,
    }
    markdown = notify.render_markdown(summary)
    assert "| b-app | ❌ failure | 0123456 | test |" in markdown
    assert "not a git repo" in markdown
    assert notify.aggregate([result("x", "success", [])])["status"] == "success"
    assert notify.aggregate([])["status"] == "failure"


def test_write_output_markdown_and_json(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MY_API_TOKEN", "supersecretvalue")
    results = sample()
    results[2]["error"] = "token supersecretvalue leaked"
    md = notify.write_output(tmp_path / "out" / "summary.md", notify.aggregate(results))
    assert "# local-ci 集計" in md.read_text()
    assert "supersecretvalue" not in md.read_text()
    data = json.loads(
        notify.write_output(tmp_path / "summary.json", notify.aggregate(results)).read_text()
    )
    assert data["repos"][2]["error"] == "token *** leaked"


class FakeResponse:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_send_discord_payload(monkeypatch) -> None:
    monkeypatch.setenv(notify.DEFAULT_WEBHOOK_ENV, WEBHOOK)
    sent = []

    def opener(request, timeout):
        sent.append((request.full_url, json.loads(request.data), timeout))
        return FakeResponse()

    results = sample()
    results[0]["repo"] = "@everyone"
    assert notify.send_discord(notify.aggregate(results), opener=opener)
    url, payload, timeout = sent[0]
    assert url == WEBHOOK and timeout == 15
    assert payload["allowed_mentions"] == {"parse": []}
    assert payload["content"].startswith("**local-ci** ❌ failure")
    assert "`@everyone` failure — test" in payload["content"]
    assert len(payload["content"]) <= notify.DISCORD_LIMIT


def test_send_discord_truncates_long_content(monkeypatch) -> None:
    monkeypatch.setenv(notify.DEFAULT_WEBHOOK_ENV, WEBHOOK)
    sent = []
    many = [result(f"repo-{index:03}-" + "x" * 40, "failure", []) for index in range(100)]
    notify.send_discord(
        notify.aggregate(many),
        opener=lambda request, timeout: sent.append(json.loads(request.data)) or FakeResponse(),
    )
    assert len(sent[0]["content"]) <= notify.DISCORD_LIMIT
    assert sent[0]["content"].endswith("(省略)")


def test_send_discord_rejects_missing_or_invalid_url(monkeypatch) -> None:
    monkeypatch.delenv(notify.DEFAULT_WEBHOOK_ENV, raising=False)
    with pytest.raises(ValueError, match="設定されていません"):
        notify.send_discord(notify.aggregate(sample()))
    for bad in ("http://discord.com/api/webhooks/1/x", "https://example.com/api/webhooks/1/x"):
        monkeypatch.setenv(notify.DEFAULT_WEBHOOK_ENV, bad)
        with pytest.raises(ValueError, match="形式が不正"):
            notify.send_discord(notify.aggregate(sample()))


def test_send_discord_error_does_not_leak_url(monkeypatch) -> None:
    monkeypatch.setenv(notify.DEFAULT_WEBHOOK_ENV, WEBHOOK)

    def opener(request, timeout):
        raise urllib.error.URLError(f"failed {WEBHOOK}")

    with pytest.raises(OSError) as error:
        notify.send_discord(notify.aggregate(sample()), opener=opener)
    assert "abcdefSECRETtoken" not in str(error.value)


def test_run_all_writes_summary_and_sends_discord(tmp_path: Path, monkeypatch, capfd) -> None:
    root = tmp_path / "projects"
    for name in ("ok", "ng"):
        (root / name / ".git").mkdir(parents=True)
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(notify.DEFAULT_WEBHOOK_ENV, WEBHOOK)
    monkeypatch.setattr(
        cli,
        "run",
        lambda repo, **kw: result(
            repo.name,
            "success" if repo.name == "ok" else "failure",
            [("test", "success" if repo.name == "ok" else "failed", False)],
        ),
    )
    sent = []
    monkeypatch.setattr(
        notify.urllib.request,
        "urlopen",
        lambda request, timeout: sent.append(json.loads(request.data)) or FakeResponse(),
    )
    output = tmp_path / "report.json"
    code = cli.main(
        ["run-all", "--root", str(root), "--quiet", "--output", str(output), "--discord"]
    )
    assert code == 1
    assert [repo["status"] for repo in json.loads(output.read_text())["repos"]] == [
        "failure",
        "success",
    ]
    assert (tmp_path / "home" / "summary" / "latest.md").exists()
    assert "`ng` failure — test" in sent[0]["content"]
    out = capfd.readouterr()
    assert "| ng | ❌ failure |" in out.out
    assert "abcdefSECRETtoken" not in out.out + out.err


def test_run_all_discord_failure_is_warning(tmp_path: Path, monkeypatch, capfd) -> None:
    root = tmp_path / "projects"
    (root / "ok" / ".git").mkdir(parents=True)
    monkeypatch.setenv("LOCAL_CI_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(notify.DEFAULT_WEBHOOK_ENV, raising=False)
    monkeypatch.setattr(cli, "run", lambda repo, **kw: result(repo.name, "success", []))
    assert cli.main(["run-all", "--root", str(root), "--quiet", "--discord"]) == 0
    assert "警告" in capfd.readouterr().err


def test_compose_repo_skips_auto_detected_e2e(tmp_path: Path) -> None:
    """testcase-manager 回帰: DB が必要な e2e を前提条件なしで実行しない。"""
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    for name, package in (
        ("frontend", {"scripts": {"test": "vitest run"}}),
        ("e2e", {"scripts": {"e2e": "playwright test"}}),
    ):
        (tmp_path / name).mkdir()
        (tmp_path / name / "package.json").write_text(json.dumps(package))
    jobs = {job.name: job for job in load_config(tmp_path).jobs}
    assert jobs["e2e:e2e"].needs_services
    assert "compose.yaml" in jobs["e2e:e2e"].skip_reason
    assert not jobs["frontend:test"].needs_services
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outcome = runner._run_job(jobs["e2e:e2e"], tmp_path, run_dir, runner.Redactor())
    assert outcome["status"] == "skipped"
    assert "before フック" in outcome["skip_reason"]
    planned = runner._planned_job(jobs["e2e:e2e"], tmp_path)
    assert planned["skip"] and "compose.yaml" in planned["skip_reason"]


def test_explicit_config_e2e_is_not_skipped_by_compose(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}\n")
    (tmp_path / ".local-ci.yml").write_text(
        "jobs:\n  browser:\n    kind: e2e\n    run: npx playwright test\n"
    )
    assert not load_config(tmp_path).jobs[0].needs_services


def test_child_env_drops_webhook(monkeypatch) -> None:
    monkeypatch.setenv(notify.DEFAULT_WEBHOOK_ENV, WEBHOOK)
    monkeypatch.setenv("SLACK_WEBHOOK", "https://hooks.example/x")
    env = runner._child_env(runner.Job("t", ["echo"], "test"))
    assert notify.DEFAULT_WEBHOOK_ENV not in env and "SLACK_WEBHOOK" not in env
    env = runner._child_env(runner.Job("t", ["echo"], "test", pass_env=["SLACK_WEBHOOK"]))
    assert "SLACK_WEBHOOK" in env
