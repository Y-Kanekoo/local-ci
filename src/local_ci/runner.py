"""専用 worktree でのジョブ実行と結果保存。"""

import codecs
import json
import os
import selectors
import shutil
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path

from local_ci.config import Config, Job, command_args, load_config
from local_ci.security import Redactor


def home() -> Path:
    return Path(os.environ.get("LOCAL_CI_HOME", Path.home() / ".local-ci")).expanduser().resolve()


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def repo_name(repo: Path) -> str:
    return repo.name.replace("/", "_")


def resolve_sha(repo: Path, ref: str | None = None, pr: int | None = None) -> tuple[str, str]:
    if pr is not None:
        info = subprocess.run(
            ["gh", "pr", "view", str(pr), "--json", "headRefOid"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        sha = json.loads(info.stdout)["headRefOid"]
        try:
            return git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}"), f"PR #{pr}"
        except subprocess.CalledProcessError:
            git(repo, "fetch", "origin", f"refs/pull/{pr}/head")
            return git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}"), f"PR #{pr}"
    label = ref or "HEAD"
    if label.startswith("-"):
        raise ValueError("不正な ref です")
    try:
        return git(repo, "rev-parse", "--verify", f"{label}^{{commit}}"), label
    except subprocess.CalledProcessError:
        git(repo, "fetch", "origin", label)
        return git(repo, "rev-parse", "--verify", "FETCH_HEAD^{commit}"), label


def _tool_available(args: list[str], cwd: Path, env: dict[str, str]) -> bool:
    executable = args[0]
    if "/" in executable:
        path = (
            (cwd / executable).resolve() if not Path(executable).is_absolute() else Path(executable)
        )
        return path.is_file() and os.access(path, os.X_OK)
    return shutil.which(executable, path=env.get("PATH")) is not None


def _child_env(job: Job) -> dict[str, str]:
    env = dict(os.environ)
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if name not in job.pass_env:
            env.pop(name, None)
    env["CI"] = "1"
    for name, reference in job.env.items():
        source = reference[2:-1]
        if source not in os.environ:
            raise ValueError(f"環境変数 {source} が設定されていません")
        if source in {"GH_TOKEN", "GITHUB_TOKEN"} and source not in job.pass_env:
            raise ValueError(f"{source} の引き渡しには pass_env が必要です")
        env[name] = os.environ[source]
    return env


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _execute(
    command: str, cwd: Path, env: dict[str, str], timeout: float, log, redactor: Redactor
) -> tuple[str, int | None, bool]:
    args = command_args(command)
    if not _tool_available(args, cwd, env):
        log.write(redactor.redact(f"ツールが見つかりません: {args[0]}\n"))
        return "skipped", None, False
    log.write(redactor.redact(f"$ {command}\n"))
    log.flush()
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    deadline = time.monotonic() + timeout
    timed_out = False
    browser_missing = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                _kill_group(process)
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 4096)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                pending += decoder.decode(chunk)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if "Executable doesn't exist" in line or "playwright install" in line:
                        browser_missing = True
                    log.write(redactor.feed_line(line + "\n"))
                    log.flush()
        pending += decoder.decode(b"", final=True)
        if pending:
            log.write(redactor.feed_line(pending))
        if timed_out:
            log.write("タイムアウトしました\n")
        process.wait()
    except BaseException:
        _kill_group(process)
        process.wait()
        raise
    finally:
        selector.close()
        process.stdout.close()
    return (
        "timeout" if timed_out else "success" if process.returncode == 0 else "failed",
        process.returncode,
        browser_missing,
    )


def _safe_log_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "-_.:" else "_" for char in name) + ".log"


def _run_job(job: Job, worktree: Path, run_dir: Path, redactor: Redactor) -> dict[str, object]:
    start = time.monotonic()
    log_path = run_dir / _safe_log_name(job.name)
    result: dict[str, object] = {
        "name": job.name,
        "kind": job.kind,
        "status": "skipped",
        "exit_code": None,
        "duration": 0.0,
        "log_path": str(log_path),
        "allow_failure": job.allow_failure,
    }
    with log_path.open("w", encoding="utf-8") as log:
        if job.skip:
            log.write("設定によりスキップしました\n")
        elif job.needs_services:
            log.write("外部サービスが必要なためスキップしました\n")
        else:
            cwd = (worktree / job.workdir).resolve()
            if not cwd.is_relative_to(worktree.resolve()) or not cwd.is_dir():
                result["status"] = "failed"
                log.write("workdir が worktree 内のディレクトリではありません\n")
            else:
                try:
                    env = _child_env(job)
                    for command in job.run:
                        remaining = job.timeout - (time.monotonic() - start)
                        if remaining <= 0:
                            result["status"] = "timeout"
                            log.write("タイムアウトしました\n")
                            break
                        status, code, browser_missing = _execute(
                            command, cwd, env, remaining, log, redactor
                        )
                        result["status"], result["exit_code"] = status, code
                        if job.kind == "e2e" and status == "failed" and browser_missing:
                            log.write(
                                "Playwright のブラウザが未導入です。"
                                "setup で npx playwright install chromium を指定してください。\n"
                            )
                        if status != "success":
                            break
                except ValueError as error:
                    result["status"] = "failed"
                    log.write(redactor.redact(f"設定エラー: {error}\n"))
    result["duration"] = round(time.monotonic() - start, 3)
    return result


def _summary(result: dict[str, object], redactor: Redactor) -> str:
    lines = [
        f"# local-ci: {result['repo']}",
        "",
        f"- ref: `{result['ref']}`",
        f"- SHA: `{result['sha']}`",
        f"- 結果: **{result['status']}**",
        "",
        "| ジョブ | 種類 | 結果 | 秒 | ログ |",
        "|---|---|---|---:|---|",
    ]
    for job in result["jobs"]:
        cells = [job["name"], job["kind"], job["status"], job["duration"], f"`{job['log_path']}`"]
        lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
    return redactor.redact("\n".join(lines) + "\n")


def run(
    repo: Path,
    ref: str | None = None,
    pr: int | None = None,
    only: set[str] | None = None,
    skip: set[str] | None = None,
    keep_worktree: bool = False,
    report: str = "none",
    dry_run_report: bool = False,
) -> dict[str, object]:
    repo = repo.expanduser().resolve()
    repo = Path(git(repo, "rev-parse", "--show-toplevel"))
    sha, label = resolve_sha(repo, ref, pr)
    short = sha[:7]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    unique = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    root = home()
    run_dir = root / "runs" / repo_name(repo) / f"{stamp}-{short}"
    if run_dir.exists():
        run_dir = root / "runs" / repo_name(repo) / f"{unique}-{short}"
    worktree = root / "worktrees" / f"{repo_name(repo)}-{short}-{unique}"
    run_dir.mkdir(parents=True)
    worktree.parent.mkdir(parents=True, exist_ok=True)
    redactor = Redactor()
    result: dict[str, object] = {
        "repo": repo_name(repo),
        "ref": label,
        "sha": sha,
        "pr": pr,
        "status": "error",
        "jobs": [],
    }
    created = False
    reporter = None
    try:
        git(repo, "worktree", "add", "--detach", str(worktree), sha)
        created = True
        config: Config = load_config(worktree)
        selected = [
            job
            for job in config.jobs
            if job.kind != "setup"
            if (only is None or job.kind in only) and (skip is None or job.kind not in skip)
        ]
        setup_jobs = [
            job
            for job in config.jobs
            if job.kind == "setup"
            and (skip is None or "setup" not in skip)
            and (selected or only is None or "setup" in only)
        ]
        jobs = setup_jobs + selected
        if not jobs:
            raise ValueError("実行対象のジョブが見つかりません")
        from local_ci.report import Reporter

        reporter = Reporter(repo, sha, pr, report, dry_run_report, redactor)
        reporter.status("overall", "pending", "local-ci を実行中")
        if config.setup and jobs and (skip is None or "setup" not in skip):
            jobs.insert(0, Job("setup", config.setup, "setup"))
        for job in jobs:
            reporter.status(job.name, "pending", "ジョブを実行中")
            job_result = _run_job(job, worktree, run_dir, redactor)
            result["jobs"].append(job_result)
            state = (
                "success"
                if job_result["status"] in {"success", "skipped"} or job.allow_failure
                else "failure"
            )
            reporter.status(job.name, state, f"ジョブ結果: {job_result['status']}")
        result["status"] = (
            "failure"
            if any(
                job["status"] in {"failed", "timeout"} and not job["allow_failure"]
                for job in result["jobs"]
            )
            else "success"
        )
        reporter.status("overall", result["status"], f"local-ci: {result['status']}")
        reporter.comment(result, run_dir)
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        if reporter is not None:
            reporter.status("overall", "error", "local-ci は中断されました")
        raise
    except Exception as error:
        result["error"] = redactor.redact(str(error))
        if reporter is not None:
            reporter.status("overall", "error", "local-ci の実行エラー")
        raise
    finally:
        (run_dir / "result.json").write_text(
            redactor.redact(json.dumps(result, ensure_ascii=False, indent=2)) + "\n",
            encoding="utf-8",
        )
        (run_dir / "summary.md").write_text(_summary(result, redactor), encoding="utf-8")
        if created and not keep_worktree:
            git(repo, "worktree", "remove", "--force", str(worktree))
    result["run_dir"] = str(run_dir)
    return result
