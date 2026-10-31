"""複数リポジトリの結果集計と、ファイル・Discord Webhook への出力。"""

import json
import os
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from local_ci.config import KIND_STAGE, STAGE_ORDER
from local_ci.security import Redactor

DEFAULT_WEBHOOK_ENV = "LOCAL_CI_DISCORD_WEBHOOK_URL"
DISCORD_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}
DISCORD_LIMIT = 2000
FAILED = {"failed", "timeout"}


def stage_summary(jobs: list[dict[str, object]]) -> list[dict[str, object]]:
    """ジョブ結果をステージ（setup・static・unit・api・e2e）ごとに集計する。

    status は、許可されていない失敗があれば failure、全件スキップなら skipped、
    それ以外は success（一部スキップを含む場合は skipped 件数で分かる）。
    """
    stages: dict[str, dict[str, object]] = {}
    for job in jobs:
        stage = str(job.get("stage") or KIND_STAGE.get(str(job.get("kind")), "unknown"))
        entry = stages.setdefault(
            stage,
            {
                "stage": stage,
                "status": "success",
                "jobs": 0,
                "success": 0,
                "failed": 0,
                "skipped": 0,
                "skip_reasons": [],
            },
        )
        entry["jobs"] += 1
        status = job.get("status")
        if status in FAILED and not job.get("allow_failure"):
            entry["failed"] += 1
        elif status == "skipped":
            entry["skipped"] += 1
            reason = job.get("skip_reason")
            if reason and reason not in entry["skip_reasons"]:
                entry["skip_reasons"].append(reason)
        else:
            entry["success"] += 1
    for entry in stages.values():
        if entry["failed"]:
            entry["status"] = "failure"
        elif entry["skipped"] == entry["jobs"]:
            entry["status"] = "skipped"
    return sorted(stages.values(), key=lambda item: STAGE_ORDER.get(item["stage"], 99))


def _job_status(job: dict[str, object]) -> str:
    if job.get("status") in FAILED and job.get("allow_failure"):
        return "failed (allowed)"
    return str(job.get("status"))


def aggregate(results: list[dict[str, object]]) -> dict[str, object]:
    """run() の結果（またはエラー情報）の一覧を集計する。"""
    repos = []
    counts: dict[str, int] = {}
    stage_counts: dict[str, dict[str, int]] = {}
    for result in sorted(results, key=lambda item: str(item.get("repo"))):
        jobs = [
            {
                "name": job["name"],
                "kind": job["kind"],
                "stage": job.get("stage")
                or job.get("phase")
                or KIND_STAGE.get(job["kind"], job["kind"]),
                "status": _job_status(job),
            }
            for job in [*result.get("hooks", []), *result.get("jobs", [])]
        ]
        failed = [job["name"] for job in jobs if job["status"] in FAILED]
        for job in jobs:
            counts[job["status"]] = counts.get(job["status"], 0) + 1
        stages = {
            entry["stage"]: entry["status"]
            for entry in (result.get("stages") or stage_summary(result.get("jobs", [])))
        }
        for stage, status in stages.items():
            bucket = stage_counts.setdefault(stage, {})
            bucket[status] = bucket.get(status, 0) + 1
        entry = {
            "repo": result.get("repo"),
            "ref": result.get("ref"),
            "sha": str(result.get("sha") or "")[:7],
            "status": result.get("status", "error"),
            "failed_jobs": failed,
            "stages": stages,
            "jobs": jobs,
            "run_dir": result.get("run_dir"),
        }
        if result.get("error"):
            entry["error"] = result["error"]
        repos.append(entry)
    overall = "success" if repos and all(r["status"] == "success" for r in repos) else "failure"
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": overall,
        "repos": repos,
        "job_counts": dict(sorted(counts.items())),
        "stage_counts": {
            stage: dict(sorted(stage_counts[stage].items()))
            for stage in sorted(stage_counts, key=lambda name: STAGE_ORDER.get(name, 99))
        },
    }


def render_markdown(summary: dict[str, object]) -> str:
    icon = {"success": "✅", "failure": "❌", "error": "⚠️", "interrupted": "⏹"}
    lines = [
        f"# local-ci 集計 ({summary['generated_at']})",
        "",
        f"全体: **{summary['status']}**",
        "",
        "| リポジトリ | 結果 | SHA | 失敗ジョブ |",
        "|---|---|---|---|",
    ]
    for repo in summary["repos"]:
        failed = ", ".join(repo["failed_jobs"]) or repo.get("error", "") or "-"
        lines.append(
            f"| {repo['repo']} | {icon.get(repo['status'], '')} {repo['status']} "
            f"| {repo['sha'] or '-'} | {failed} |"
        )
    stage_names = list(summary.get("stage_counts", {}))
    if stage_names:
        lines.extend(
            [
                "",
                "## ステージ別",
                "",
                "| リポジトリ | " + " | ".join(stage_names) + " |",
                "|---|" + "---|" * len(stage_names),
            ]
        )
        mark = {"success": "✅", "failure": "❌", "skipped": "⏭"}
        for repo in summary["repos"]:
            cells = [
                f"{mark.get(repo.get('stages', {}).get(name, ''), '')} "
                f"{repo.get('stages', {}).get(name, '-')}".strip()
                for name in stage_names
            ]
            lines.append(f"| {repo['repo']} | " + " | ".join(cells) + " |")
        totals = "; ".join(
            f"{name}: " + ", ".join(f"{status} {count}" for status, count in statuses.items())
            for name, statuses in summary["stage_counts"].items()
        )
        lines.extend(["", f"ステージ集計: {totals}"])
    counts = ", ".join(f"{name}: {value}" for name, value in summary["job_counts"].items())
    lines.extend(["", f"ジョブ集計: {counts or 'なし'}"])
    return "\n".join(lines) + "\n"


def render_discord(summary: dict[str, object]) -> str:
    icon = {"success": "✅", "failure": "❌", "error": "⚠️", "interrupted": "⏹"}
    lines = [f"**local-ci** {icon.get(summary['status'], '')} {summary['status']}"]
    for repo in summary["repos"]:
        detail = ", ".join(repo["failed_jobs"]) or repo.get("error", "")
        line = f"{icon.get(repo['status'], '•')} `{repo['repo']}` {repo['status']}"
        lines.append(f"{line} — {detail}" if detail else line)
    text = "\n".join(lines)
    if len(text) > DISCORD_LIMIT:
        text = text[: DISCORD_LIMIT - 20].rstrip() + "\n…(省略)"
    return text


def write_output(path: Path, summary: dict[str, object], redactor: Redactor | None = None) -> Path:
    """拡張子が .json なら JSON、それ以外は Markdown で書き出す。"""
    redactor = redactor or Redactor()
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix.lower() == ".json":
        text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    else:
        text = render_markdown(summary)
    path.write_text(redactor.redact(text), encoding="utf-8")
    return path


def validate_webhook(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in DISCORD_HOSTS
        or not parts.path.startswith("/api/webhooks/")
    ):
        raise ValueError(
            "Discord Webhook URL の形式が不正です (https://discord.com/api/webhooks/...)"
        )


def send_discord(
    summary: dict[str, object],
    env_name: str = DEFAULT_WEBHOOK_ENV,
    redactor: Redactor | None = None,
    opener=None,
) -> bool:
    """環境変数の Webhook URL へ送信する。失敗は警告扱いで False を返す。"""
    redactor = redactor or Redactor()
    url = os.environ.get(env_name, "").strip()
    if not url:
        raise ValueError(f"環境変数 {env_name} に Discord Webhook URL が設定されていません")
    validate_webhook(url)
    redactor.secrets = sorted({*redactor.secrets, url}, key=len, reverse=True)
    body = json.dumps(
        {
            "username": "local-ci",
            "content": redactor.redact(render_discord(summary)),
            "allowed_mentions": {"parse": []},
        }
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "local-ci"},
        method="POST",
    )
    try:
        with (opener or urllib.request.urlopen)(request, timeout=15) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError) as error:
        raise OSError(redactor.redact(f"Discord への送信に失敗しました: {error}")) from None
