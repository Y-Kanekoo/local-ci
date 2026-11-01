"""専用 worktree でのジョブ実行と結果保存。"""

import codecs
import json
import os
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from local_ci.config import STAGE_ORDER, STAGES, Config, Job, command_args, load_config
from local_ci.notify import FAILED, stage_summary
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
    virtual_env = env.get("VIRTUAL_ENV")
    if virtual_env and "PATH" in env:
        virtual_bin = os.path.normpath(os.path.join(virtual_env, "bin"))
        env["PATH"] = os.pathsep.join(
            part for part in env["PATH"].split(os.pathsep) if os.path.normpath(part) != virtual_bin
        )
    for name in (
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PROMPT",
        "PYTHONHOME",
        "PYTHONPATH",
        "CONDA_PREFIX",
        "CONDA_DEFAULT_ENV",
        "UV_PROJECT_ENVIRONMENT",
    ):
        env.pop(name, None)
    for name in list(env):
        if name.startswith(("npm_config_", "npm_lifecycle_", "npm_package_")):
            env.pop(name)
    for name in list(env):
        if (name in {"GH_TOKEN", "GITHUB_TOKEN"} or "WEBHOOK" in name.upper()) and (
            name not in job.pass_env
        ):
            env.pop(name, None)
    env["CI"] = "1"
    if job.kind == "e2e":
        env["PLAYWRIGHT_FORCE_TTY"] = "0"
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
) -> tuple[str, int | None, bool, bool]:
    args = command_args(command)
    if not _tool_available(args, cwd, env):
        log.write(redactor.redact(f"ツールが見つかりません: {args[0]}\n"))
        return "skipped", None, False, False
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
    webserver_failed = False
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
                    if "config.webServer" in line:
                        webserver_failed = True
                    log.write(redactor.feed_line(line + "\n"))
                    log.flush()
        pending += decoder.decode(b"", final=True)
        if pending:
            if "config.webServer" in pending:
                webserver_failed = True
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
        webserver_failed,
    )


def _safe_log_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "__" for char in name) + ".log"


def _run_job(
    job: Job,
    worktree: Path,
    run_dir: Path,
    redactor: Redactor,
    template: dict[str, str] | None = None,
    forced_skip: str | None = None,
) -> dict[str, object]:
    start = time.monotonic()
    log_name = f"{job.kind}:{job.name}" if job.kind in {"before", "after"} else job.name
    log_path = run_dir / _safe_log_name(log_name)
    result: dict[str, object] = {
        "name": job.name,
        "kind": job.kind,
        "stage": job.stage,
        "status": "skipped",
        "exit_code": None,
        "duration": 0.0,
        "log_path": str(log_path),
        "allow_failure": job.allow_failure,
    }
    if job.kind in {"before", "after"}:
        result["phase"] = job.kind
    with log_path.open("w", encoding="utf-8") as log:
        if forced_skip or job.skip or job.needs_services:
            reason = forced_skip or (
                (job.skip_reason or "設定によりスキップしました")
                if job.skip
                else job.skip_reason or "外部サービスが必要なためスキップしました"
            )
            result["skip_reason"] = reason
            log.write(redactor.redact(reason + "\n"))
        else:
            cwd = (worktree / job.workdir).resolve()
            if not cwd.is_relative_to(worktree.resolve()) or not cwd.is_dir():
                result["status"] = "failed"
                log.write("workdir が worktree 内のディレクトリではありません\n")
            else:
                try:
                    env = _child_env(job)
                    for command in job.run:
                        if template:
                            for key, value in template.items():
                                command = command.replace("{" + key + "}", shlex.quote(value))
                        remaining = job.timeout - (time.monotonic() - start)
                        if remaining <= 0:
                            result["status"] = "timeout"
                            log.write("タイムアウトしました\n")
                            break
                        status, code, browser_missing, webserver_failed = _execute(
                            command, cwd, env, remaining, log, redactor
                        )
                        result["status"], result["exit_code"] = status, code
                        if job.kind == "e2e" and status == "failed" and browser_missing:
                            log.write(
                                "Playwright のブラウザが未導入です。"
                                "setup で npx playwright install chromium を指定してください。\n"
                            )
                        if (
                            job.kind == "e2e"
                            and status in {"failed", "timeout"}
                            and webserver_failed
                        ):
                            result["hint"] = (
                                "webServer 起動失敗の場合は DB 等の依存サービスを "
                                "before フックで起動してください"
                            )
                        if status != "success":
                            break
                except ValueError as error:
                    result["status"] = "failed"
                    log.write(redactor.redact(f"設定エラー: {error}\n"))
                except OSError as error:
                    result["status"] = "failed"
                    log.write(redactor.redact(f"実行エラー: {error}\n"))
    result["duration"] = round(time.monotonic() - start, 3)
    return result


def _summary(result: dict[str, object], redactor: Redactor) -> str:
    lines = [
        f"# local-ci: {result['repo']}",
        "",
        f"- ref: `{result['ref']}`",
        f"- SHA: `{result['sha']}`",
        f"- config_source: `{result['config_source']}`",
        f"- 結果: **{result['status']}**",
        "",
        "## ステージ別",
        "",
        "| ステージ | 結果 | ジョブ | 成功 | 失敗 | スキップ | スキップ理由 |",
        "|---|---|---:|---:|---:|---:|---|",
    ]
    for entry in result.get("stages") or stage_summary(result["jobs"]):
        reasons = " / ".join(entry["skip_reasons"]) or "-"
        lines.append(
            f"| {entry['stage']} | {entry['status']} | {entry['jobs']} | {entry['success']} "
            f"| {entry['failed']} | {entry['skipped']} | {reasons} |"
        )
    lines.extend(
        [
            "",
            "## ジョブ",
            "",
            "| ジョブ | ステージ | 種類 | 結果 | 秒 | ログ |",
            "|---|---|---|---|---:|---|",
        ]
    )
    before = [hook for hook in result["hooks"] if hook["phase"] == "before"]
    after = [hook for hook in result["hooks"] if hook["phase"] == "after"]
    hints = []
    for job in [*before, *result["jobs"], *after]:
        status = (
            "failed (allowed)"
            if job["status"] == "failed" and job["allow_failure"]
            else job["status"]
        )
        cells = [
            job["name"],
            job.get("stage", job["kind"]),
            job["kind"],
            status,
            job["duration"],
            f"`{job['log_path']}`",
        ]
        lines.append("| " + " | ".join(str(cell) for cell in cells) + " |")
        if job.get("hint"):
            hints.append(str(job["hint"]))
    lines.extend(["", *hints])
    return redactor.redact("\n".join(lines) + "\n")


def _progress(
    job: Job, result: dict[str, object] | None, repo: str | None, quiet: bool, redactor: Redactor
) -> None:
    if quiet:
        return
    prefix = f"[local-ci] {repo}: " if repo else "[local-ci] "
    if result is None:
        message = f"{prefix}▶ {job.name}"
    else:
        status = result["status"]
        symbol = "-" if status == "skipped" else "✓" if status == "success" else "✗"
        suffix = " failed (allowed)" if status == "failed" and job.allow_failure else ""
        message = f"{prefix}{symbol} {job.name} ({result['duration']:.1f}s){suffix}"
    print(redactor.redact(message), file=sys.stderr, flush=True)


def _planned_job(job: Job, worktree: Path) -> dict[str, object]:
    reason = None
    if job.skip:
        reason = job.skip_reason or "設定によりスキップします"
    elif job.needs_services:
        reason = job.skip_reason or "外部サービスが必要なためスキップします"
    else:
        cwd = (worktree / job.workdir).resolve()
        if cwd.is_relative_to(worktree.resolve()) and cwd.is_dir():
            try:
                env = _child_env(job)
            except ValueError:
                env = dict(os.environ)
            for command in job.run:
                args = command_args(command)
                if not _tool_available(args, cwd, env):
                    reason = f"ツールが見つかりません: {args[0]}"
                    break
    return {
        "name": job.name,
        "kind": job.kind,
        "stage": job.stage,
        "workdir": job.workdir,
        "run": job.run,
        "skip": reason is not None,
        "skip_reason": reason,
    }


def plan(repo: Path, ref: str | None = None) -> dict[str, object]:
    repo = Path(git(repo.expanduser().resolve(), "rev-parse", "--show-toplevel"))
    sha, label = resolve_sha(repo, ref)
    root = home()
    (root / "worktrees").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="plan-", dir=root / "worktrees") as temp:
        worktree = Path(temp) / "source"
        created = False
        try:
            git(repo, "worktree", "add", "--detach", str(worktree), sha)
            created = True
            config = load_config(worktree, root / "repos" / f"{repo_name(repo)}.yml")
            jobs = list(config.jobs)
            if config.setup:
                jobs.insert(0, Job("setup", config.setup, "setup"))
            result = {
                "repo": repo_name(repo),
                "ref": label,
                "sha": sha,
                "config_source": config.source,
                "fail_fast": config.fail_fast,
                "jobs": [_planned_job(job, worktree) for job in jobs],
                "before": [_planned_job(job, worktree) for job in config.before],
                "after": [_planned_job(job, worktree) for job in config.after],
            }
            return json.loads(Redactor().redact(json.dumps(result, ensure_ascii=False)))
        finally:
            if created:
                git(repo, "worktree", "remove", "--force", str(worktree))


def run(
    repo: Path,
    ref: str | None = None,
    pr: int | None = None,
    only: set[str] | None = None,
    skip: set[str] | None = None,
    keep_worktree: bool = False,
    report: str = "none",
    dry_run_report: bool = False,
    quiet: bool = False,
    progress_repo: bool = False,
    stages: set[str] | None = None,
    fail_fast: bool | None = None,
    config_path: Path | None = None,
) -> dict[str, object]:
    """ref の専用 worktree でジョブを実行する。

    config_path を指定すると、ref の .local-ci.yml や外部設定の代わりにその設定を使う。

    stages を指定すると、その段階（static・unit・api・e2e）のジョブと setup だけを実行する。
    fail_fast（None なら設定の fail_fast）が真なら、許可されていない失敗が出た段階より
    後の段階のジョブを理由付きでスキップする。同じ段階の残りのジョブは実行する。
    """
    if stages is not None and (not stages or set(stages) - set(STAGES)):
        raise ValueError(f"stage は {', '.join(STAGES)} から指定してください")
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
        "hooks": [],
        "config_source": "auto",
    }
    created = False
    reporter = None
    try:
        git(repo, "worktree", "add", "--detach", str(worktree), sha)
        created = True
        config: Config = load_config(
            worktree, root / "repos" / f"{repo_name(repo)}.yml", config_path
        )
        result["config_source"] = config.source
        template = {
            "repo": repo_name(repo),
            "sha": sha,
            "short_sha": short,
            "worktree": str(worktree),
        }
        selected = [
            job
            for job in config.jobs
            if job.kind != "setup"
            if (only is None or job.kind in only) and (skip is None or job.kind not in skip)
            if stages is None or job.stage in stages
        ]
        jobs = [
            job
            for job in config.jobs
            if (job.kind != "setup" and job in selected)
            or (
                job.kind == "setup"
                and (skip is None or "setup" not in skip)
                and (selected or (only is None and stages is None) or (only and "setup" in only))
            )
        ]
        if not jobs:
            raise ValueError("実行対象のジョブが見つかりません")
        from local_ci.report import Reporter

        reporter = Reporter(repo, sha, pr, report, dry_run_report, redactor)
        reporter.status("overall", "pending", "local-ci を実行中")
        if config.setup and jobs and (skip is None or "setup" not in skip):
            jobs.insert(0, Job("setup", config.setup, "setup"))
        progress_name = repo_name(repo) if progress_repo else None
        stop_early = config.fail_fast if fail_fast is None else fail_fast
        result["fail_fast"] = stop_early
        failed_stage: str | None = None
        before_failed = False
        try:
            for hook in config.before:
                _progress(hook, None, progress_name, quiet, redactor)
                hook_result = _run_job(
                    hook,
                    worktree,
                    run_dir,
                    redactor,
                    template,
                    "before フックが失敗したためスキップしました" if before_failed else None,
                )
                result["hooks"].append(hook_result)
                _progress(hook, hook_result, progress_name, quiet, redactor)
                if hook_result["status"] != "success":
                    before_failed = True
            for job in jobs:
                reporter.status(job.name, "pending", "ジョブを実行中")
                _progress(job, None, progress_name, quiet, redactor)
                forced = None
                if before_failed:
                    forced = "before フックが失敗したためスキップしました"
                elif failed_stage is not None and STAGE_ORDER.get(job.stage, 99) > STAGE_ORDER.get(
                    failed_stage, 99
                ):
                    forced = (
                        f"前のステージ {failed_stage} が失敗したためスキップしました（fail_fast）"
                    )
                job_result = _run_job(job, worktree, run_dir, redactor, template, forced)
                result["jobs"].append(job_result)
                if (
                    stop_early
                    and failed_stage is None
                    and job_result["status"] in FAILED
                    and not job.allow_failure
                ):
                    failed_stage = job.stage
                _progress(job, job_result, progress_name, quiet, redactor)
                state = (
                    "success"
                    if job_result["status"] in {"success", "skipped"} or job.allow_failure
                    else "failure"
                )
                reporter.status(job.name, state, f"ジョブ結果: {job_result['status']}")
            result["status"] = (
                "failure"
                if before_failed
                or any(
                    job["status"] in {"failed", "timeout"} and not job["allow_failure"]
                    for job in result["jobs"]
                )
                else "success"
            )
        finally:
            result["stages"] = stage_summary(result["jobs"])
            for hook in config.after:
                _progress(hook, None, progress_name, quiet, redactor)
                try:
                    hook_result = _run_job(hook, worktree, run_dir, redactor, template)
                    result["hooks"].append(hook_result)
                    _progress(hook, hook_result, progress_name, quiet, redactor)
                except Exception as error:
                    result["hooks"].append(
                        {
                            "name": hook.name,
                            "kind": "after",
                            "phase": "after",
                            "status": "failed",
                            "exit_code": None,
                            "duration": 0.0,
                            "log_path": str(run_dir / _safe_log_name(f"after:{hook.name}")),
                            "allow_failure": False,
                            "error": redactor.redact(str(error)),
                        }
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
