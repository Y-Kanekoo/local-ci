"""gh CLI による任意の GitHub 報告。"""

import json
import re
import subprocess
import sys
from pathlib import Path

from local_ci.security import Redactor

MARKER = "<!-- local-ci -->"


def _comment_pages(value: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    comments = []
    offset = 0
    while offset < len(value):
        while offset < len(value) and value[offset].isspace():
            offset += 1
        if offset == len(value):
            break
        chunk, end = decoder.raw_decode(value, offset)
        if isinstance(chunk, list):
            comments.extend(item for item in chunk if isinstance(item, dict))
        offset = end
        while offset < len(value) and value[offset].isspace():
            offset += 1
    return comments


def _remote(repo: Path) -> str:
    value = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?$", value)
    if not match:
        raise ValueError("GitHub の origin が見つかりません")
    return f"{match.group(1)}/{match.group(2)}"


class Reporter:
    def __init__(
        self, repo: Path, sha: str, pr: int | None, mode: str, dry_run: bool, redactor: Redactor
    ) -> None:
        self.repo, self.sha, self.pr = repo, sha, pr
        self.mode, self.dry_run, self.redactor = mode, dry_run, redactor
        self.owner_repo: str | None = None

    def _owner_repo(self) -> str:
        if self.owner_repo is None:
            self.owner_repo = _remote(self.repo)
        return self.owner_repo

    def _gh(self, args: list[str]) -> str | None:
        try:
            if self.dry_run:
                print(self.redactor.redact("送信予定: gh " + " ".join(args)))
                return None
            result = subprocess.run(
                ["gh", *args], cwd=self.repo, capture_output=True, text=True, check=True
            )
            return result.stdout
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            print(
                self.redactor.redact(f"警告: GitHub 報告に失敗しました: {error}"), file=sys.stderr
            )
            return None

    def status(self, name: str, state: str, description: str) -> None:
        if self.mode != "status":
            return
        try:
            endpoint = f"repos/{self._owner_repo()}/statuses/{self.sha}"
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            print(
                self.redactor.redact(f"警告: GitHub 報告に失敗しました: {error}"), file=sys.stderr
            )
            return
        self._gh(
            [
                "api",
                endpoint,
                "-f",
                f"state={state}",
                "-f",
                f"context=local-ci/{name}",
                "-f",
                f"description={self.redactor.redact(description)[:140]}",
            ]
        )

    def comment(self, result: dict[str, object], run_dir: Path) -> None:
        if self.mode != "comment" or self.pr is None:
            return
        try:
            owner_repo = self._owner_repo()
        except (OSError, subprocess.CalledProcessError, ValueError) as error:
            print(
                self.redactor.redact(f"警告: GitHub 報告に失敗しました: {error}"), file=sys.stderr
            )
            return
        lines = [
            MARKER,
            f"## local-ci: {result['status']}",
            "",
            f"コミット: `{result['sha']}`",
            "",
            "| ジョブ | 結果 | 秒 |",
            "|---|---|---:|",
        ]
        for job in result["jobs"]:
            lines.append(f"| {job['name']} | {job['status']} | {job['duration']} |")
        for job in result["jobs"]:
            if job["status"] in {"failed", "timeout"}:
                path = Path(job["log_path"])
                tail = path.read_text(encoding="utf-8").splitlines()[-40:]
                lines.extend(["", f"### {job['name']} のログ末尾", "```text", *tail, "```"])
        lines.extend(["", f"ローカルログ: `{run_dir}`"])
        body = self.redactor.redact("\n".join(lines))
        if self.dry_run:
            print(body)
            return
        comments = self._gh(["api", f"repos/{owner_repo}/issues/{self.pr}/comments", "--paginate"])
        if comments:
            try:
                existing = next(
                    (item for item in _comment_pages(comments) if MARKER in item.get("body", "")),
                    None,
                )
            except (ValueError, TypeError):
                existing = None
            if existing and existing.get("id") is not None:
                updated = self._gh(
                    [
                        "api",
                        f"repos/{owner_repo}/issues/comments/{existing['id']}",
                        "-X",
                        "PATCH",
                        "-f",
                        f"body={body}",
                    ]
                )
                if updated is not None:
                    return
        self._gh(["pr", "comment", str(self.pr), "-R", owner_repo, "--body", body])
