import json
from pathlib import Path
from types import SimpleNamespace

from local_ci import report
from local_ci.security import Redactor


def test_status_and_comment_update(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(report, "_remote", lambda _: "owner/repo")
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if "--paginate" in args:
            return SimpleNamespace(stdout=json.dumps([{"id": 9, "body": report.MARKER}]))
        return SimpleNamespace(stdout="{}")

    monkeypatch.setattr(report.subprocess, "run", fake_run)
    reporter = report.Reporter(tmp_path, "abc123", 3, "status", False, Redactor({}))
    reporter.status("test", "pending", "実行中")
    assert calls[0][:3] == ["gh", "api", "repos/owner/repo/statuses/abc123"]
    log = tmp_path / "test.log"
    log.write_text("failure\n")
    reporter.mode = "comment"
    reporter.comment(
        {
            "status": "failure",
            "sha": "abc123",
            "jobs": [{"name": "test", "status": "failed", "duration": 1.0, "log_path": str(log)}],
        },
        tmp_path,
    )
    assert any("PATCH" in call for call in calls)
    assert "failure" in calls[-1][-1]
