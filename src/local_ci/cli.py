"""コマンドライン操作。"""

import argparse
import fnmatch
import json
import plistlib
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

from local_ci.runner import git, home, plan, run
from local_ci.security import Redactor


def _global_config() -> dict[str, object]:
    path = home() / "config.yml"
    if not path.exists():
        return {}
    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, dict):
        raise ValueError("グローバル設定はマッピングで指定してください")
    return value


def _patterns(value: object) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else list(value)


def scan_repos(
    root: Path, include: list[str] | None = None, exclude: list[str] | None = None
) -> list[Path]:
    if not root.is_dir():
        raise ValueError(f"ルートディレクトリが見つかりません: {root}")
    found = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / ".git").is_dir():
            continue
        if include and not any(fnmatch.fnmatch(child.name, pattern) for pattern in include):
            continue
        if exclude and any(fnmatch.fnmatch(child.name, pattern) for pattern in exclude):
            continue
        found.append(child)
    return found


def _run_all(args: argparse.Namespace) -> int:
    config = _global_config()
    root = Path(args.root or config.get("root") or "~/Projects").expanduser()
    include = args.include or _patterns(config.get("include"))
    exclude = args.exclude or _patterns(config.get("exclude"))
    report = args.report or str(config.get("report", "none"))
    workers = args.jobs or int(config.get("jobs", 1))
    if workers < 1:
        raise ValueError("--jobs は 1 以上を指定してください")
    repos = scan_repos(root, include, exclude)
    results = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(run, repo, report=report, quiet=args.quiet, progress_repo=True): repo
            for repo in repos
        }
        for future in as_completed(futures):
            repo = futures[future]
            try:
                result = future.result()
                results.append((repo.name, result["status"]))
            except Exception as error:
                results.append((repo.name, "error"))
                print(Redactor().redact(f"警告: {repo.name}: {error}"), file=sys.stderr)
    print("| リポジトリ | 結果 |\n|---|---|")
    for name, status in sorted(results):
        print(f"| {name} | {status} |")
    return 1 if any(status not in {"success"} for _, status in results) else 0


def _watch(args: argparse.Namespace) -> int:
    import time

    config = _global_config()
    root = Path(args.root or config.get("root") or "~/Projects").expanduser()
    configured = config.get("watch_repos")
    if configured:
        repos = [Path(item).expanduser().resolve() for item in configured]
    else:
        repos = scan_repos(root, _patterns(config.get("include")), _patterns(config.get("exclude")))
    state_path = home() / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    branches = args.branches or _patterns(config.get("branches"))
    report = args.report or str(config.get("report", "none"))
    while True:
        for repo in repos:
            if not (repo / ".git").exists():
                continue
            try:
                output = subprocess.run(
                    ["gh", "pr", "list", "--json", "number,headRefOid,headRefName"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                prs = json.loads(output.stdout)
            except (OSError, subprocess.CalledProcessError, ValueError) as error:
                print(
                    Redactor().redact(f"警告: {repo.name} の PR 取得に失敗: {error}"),
                    file=sys.stderr,
                )
                prs = []
            for pr in prs:
                key = f"{repo}:pr:{pr['number']}"
                sha = pr["headRefOid"]
                if state.get(key) == sha:
                    continue
                try:
                    result = run(repo, pr=pr["number"], report=report)
                    state[key] = sha
                    print(f"{repo.name} PR #{pr['number']}: {result['status']}")
                except Exception as error:
                    print(Redactor().redact(f"警告: {key}: {error}"), file=sys.stderr)
                state_path.parent.mkdir(parents=True, exist_ok=True)
                state_path.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
            for branch in branches:
                try:
                    git(repo, "fetch", "origin", branch)
                    sha = git(repo, "rev-parse", "FETCH_HEAD^{commit}")
                except (OSError, subprocess.CalledProcessError) as error:
                    print(
                        Redactor().redact(
                            f"警告: {repo.name} の {branch} を取得できません: {error}"
                        ),
                        file=sys.stderr,
                    )
                    continue
                key = f"{repo}:branch:{branch}"
                if state.get(key) != sha:
                    try:
                        result = run(repo, ref=sha, report=report)
                        state[key] = sha
                        print(f"{repo.name} {branch}: {result['status']}")
                    except Exception as error:
                        print(Redactor().redact(f"警告: {key}: {error}"), file=sys.stderr)
                    state_path.parent.mkdir(parents=True, exist_ok=True)
                    state_path.write_text(
                        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                    )
        if args.once:
            return 0
        time.sleep(args.interval)


def _plist(command: str, interval: int) -> bytes:
    executable = shutil.which("local-ci") or str(Path(sys.argv[0]).resolve())
    args = shlex.split(command)
    if not args:
        raise ValueError("--command が空です")
    paths = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        str(Path.home() / ".local" / "bin"),
        "/usr/bin",
        "/bin",
        str(Path(executable).parent),
    ]
    payload = {
        "Label": "io.github.y-kanekoo.local-ci",
        "ProgramArguments": [executable, *args],
        "StartInterval": interval,
        "RunAtLoad": False,
        "EnvironmentVariables": {"PATH": ":".join(dict.fromkeys(paths)), "HOME": str(Path.home())},
        "StandardOutPath": str(home() / "launchd.log"),
        "StandardErrorPath": str(home() / "launchd-error.log"),
    }
    return plistlib.dumps(payload, sort_keys=False)


def _launchd(args: argparse.Namespace) -> int:
    path = Path.home() / "Library" / "LaunchAgents" / "io.github.y-kanekoo.local-ci.plist"
    if args.action == "install":
        if args.interval <= 0:
            raise ValueError("--interval は正の整数で指定してください")
        data = _plist(args.launch_command, args.interval)
        if args.print_plist:
            sys.stdout.buffer.write(data)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"作成しました: {path}")
            print(f"有効化: launchctl bootstrap gui/$(id -u) {shlex.quote(str(path))}")
            print(f"無効化: launchctl bootout gui/$(id -u) {shlex.quote(str(path))}")
    else:
        print(f"先に無効化してください: launchctl bootout gui/$(id -u) {shlex.quote(str(path))}")
        path.unlink(missing_ok=True)
        print(f"削除しました: {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="local-ci", description="Mac 上で CI を実行します")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="専用 worktree で実行")
    run_parser.add_argument("repo", type=Path)
    ref_group = run_parser.add_mutually_exclusive_group()
    ref_group.add_argument("--ref")
    ref_group.add_argument("--pr", type=int)
    run_parser.add_argument("--only", help="対象 kind をカンマ区切りで指定")
    run_parser.add_argument("--skip", help="除外 kind をカンマ区切りで指定")
    run_parser.add_argument("--keep-worktree", action="store_true")
    run_parser.add_argument("--report", choices=["none", "status", "comment"], default="none")
    run_parser.add_argument("--dry-run-report", action="store_true")
    run_parser.add_argument("--quiet", action="store_true", help="進捗表示を抑制")
    plan_parser = commands.add_parser("plan", help="ジョブの実行計画を表示")
    plan_parser.add_argument("repo", type=Path)
    plan_parser.add_argument("--ref")
    plan_parser.add_argument("--json", action="store_true")
    logs = commands.add_parser("logs", help="最新の結果を表示")
    logs.add_argument("repo")
    all_parser = commands.add_parser("run-all", help="複数リポジトリを実行")
    all_parser.add_argument("--root")
    all_parser.add_argument("--include", action="append")
    all_parser.add_argument("--exclude", action="append")
    all_parser.add_argument("--jobs", type=int)
    all_parser.add_argument("--report", choices=["none", "status", "comment"])
    all_parser.add_argument("--quiet", action="store_true", help="進捗表示を抑制")
    watch = commands.add_parser("watch", help="PR とブランチの新コミットを監視")
    watch.add_argument("--once", action="store_true")
    watch.add_argument("--root")
    watch.add_argument("--branches", action="append")
    watch.add_argument("--report", choices=["none", "status", "comment"])
    watch.add_argument("--interval", type=int, default=900)
    launchd = commands.add_parser("launchd", help="launchd 設定を出力・作成")
    launchd_sub = launchd.add_subparsers(dest="action", required=True)
    install = launchd_sub.add_parser("install")
    install.add_argument("--interval", type=int, default=900)
    install.add_argument("--command", dest="launch_command", default="watch --once")
    install.add_argument("--print", dest="print_plist", action="store_true")
    launchd_sub.add_parser("uninstall")
    cron = commands.add_parser("cron", help="cron 設定を表示")
    cron_sub = cron.add_subparsers(dest="action", required=True)
    cron_sub.add_parser("print")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            only = set(args.only.split(",")) if args.only else None
            skip = set(args.skip.split(",")) if args.skip else None
            result = run(
                args.repo,
                args.ref,
                args.pr,
                only,
                skip,
                args.keep_worktree,
                args.report,
                args.dry_run_report,
                args.quiet,
            )
            print(f"{result['repo']}: {result['status']} ({result['run_dir']})")
            return 0 if result["status"] == "success" else 1
        if args.command == "plan":
            result = plan(args.repo, args.ref)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"{result['repo']} ({result['ref']}, {result['sha'][:7]})")
                print(f"config_source: {result['config_source']}")
                for phase in ("before", "jobs", "after"):
                    for job in result[phase]:
                        reason = f" [skip: {job['skip_reason']}]" if job["skip"] else ""
                        print(
                            f"{job['name']} | {job['kind']} | {job['workdir']} | "
                            f"{' ; '.join(job['run'])}{reason}"
                        )
            return 0
        if args.command == "logs":
            root = home() / "runs" / Path(args.repo).name
            dirs = (
                sorted((item for item in root.iterdir() if item.is_dir()), reverse=True)
                if root.exists()
                else []
            )
            if not dirs:
                raise ValueError("実行結果が見つかりません")
            print((dirs[0] / "summary.md").read_text(encoding="utf-8"))
            return 0
        if args.command == "run-all":
            return _run_all(args)
        if args.command == "watch":
            return _watch(args)
        if args.command == "launchd":
            return _launchd(args)
        if args.command == "cron":
            executable = shutil.which("local-ci") or str(Path(sys.argv[0]).resolve())
            path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
            print(f"*/15 * * * * PATH={path} {shlex.quote(executable)} watch --once")
            return 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(Redactor().redact(f"エラー: {error}"), file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
