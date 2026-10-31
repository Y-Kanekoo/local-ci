"""設定読み込みとリポジトリの自動検出。"""

import json
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

KINDS = {"lint", "typecheck", "test", "e2e", "build", "setup"}
MARKERS = {
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "CMakeLists.txt",
    "pom.xml",
    "mvnw",
    "go.mod",
}
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")
UNSAFE = {"rm", "rmdir", "sudo", "doas", "launchctl", "mkfs", "dd"}


@dataclass
class Job:
    name: str
    run: list[str]
    kind: str
    workdir: str = "."
    env: dict[str, str] = field(default_factory=dict)
    timeout: int = 1800
    needs_services: bool = False
    allow_failure: bool = False
    skip: bool = False
    pass_env: list[str] = field(default_factory=list)


@dataclass
class Config:
    jobs: list[Job]
    setup: list[str] = field(default_factory=list)


def command_args(command: str) -> list[str]:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("run には空でないコマンド文字列を指定してください")
    args = shlex.split(command)
    if (
        not args
        or Path(args[0]).name in UNSAFE
        or (
            Path(args[0]).name == "git"
            and any(item in {"clean", "reset", "checkout", "prune"} for item in args[1:])
        )
    ):
        raise ValueError(f"破壊的なコマンドは実行できません: {command}")
    if any(token in {";", "|", "&&", "||", ">", ">>", "<"} for token in args):
        raise ValueError(f"シェル演算子は使えません: {command}")
    return args


def _commands(value: object) -> list[str]:
    commands = [value] if isinstance(value, str) else value
    if (
        not isinstance(commands, list)
        or not commands
        or not all(isinstance(item, str) for item in commands)
    ):
        raise ValueError("run と setup はコマンド文字列またはその配列で指定してください")
    for command in commands:
        command_args(command)
    return commands


def _job(name: str, raw: object) -> Job:
    if not isinstance(raw, dict):
        raise ValueError(f"ジョブ {name} はマッピングで指定してください")
    kind = raw.get("kind", name.split(":")[-1])
    if kind not in KINDS:
        raise ValueError(f"ジョブ {name} の kind が不正です: {kind}")
    env = raw.get("env", {})
    if not isinstance(env, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) or not ENV_REF.fullmatch(value)
        for key, value in env.items()
    ):
        raise ValueError(f"ジョブ {name} の env は ${{ENV_VAR}} 参照のみ使用できます")
    pass_env = raw.get("pass_env", [])
    if not isinstance(pass_env, list) or any(not isinstance(item, str) for item in pass_env):
        raise ValueError(f"ジョブ {name} の pass_env は変数名の配列で指定してください")
    timeout = raw.get("timeout", 1800)
    if not isinstance(timeout, int) or timeout <= 0:
        raise ValueError(f"ジョブ {name} の timeout は正の整数で指定してください")
    workdir = raw.get("workdir", ".")
    if not isinstance(workdir, str) or Path(workdir).is_absolute() or ".." in Path(workdir).parts:
        raise ValueError(f"ジョブ {name} の workdir は相対パスで指定してください")
    return Job(
        name,
        _commands(raw.get("run")),
        kind,
        workdir,
        env,
        timeout,
        bool(raw.get("needs_services", False)),
        bool(raw.get("allow_failure", False)),
        bool(raw.get("skip", False)),
        pass_env,
    )


def load_config(root: Path) -> Config:
    auto = detect(root)
    path = root / ".local-ci.yml"
    if not path.exists():
        return Config(auto)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("jobs", {}), dict):
        raise ValueError(".local-ci.yml の jobs はマッピングで指定してください")
    jobs = [_job(name, value) for name, value in raw.get("jobs", {}).items()]
    if raw.get("include_auto", False):
        existing = {job.name for job in jobs}
        jobs.extend(job for job in auto if job.name not in existing)
    setup = _commands(raw["setup"]) if "setup" in raw else []
    return Config(jobs, setup)


def _node(root: Path, prefix: str) -> list[Job]:
    path = root / "package.json"
    if not path.exists():
        return []
    package = json.loads(path.read_text(encoding="utf-8"))
    manager, install = ("npm", "npm ci")
    if (root / "pnpm-lock.yaml").exists():
        manager, install = "pnpm", "pnpm install --frozen-lockfile"
    elif (root / "yarn.lock").exists():
        manager, install = "yarn", "yarn install --frozen-lockfile"
    elif not (root / "package-lock.json").exists():
        install = "npm install"
    jobs = [
        Job(
            prefix + "install",
            [install],
            "setup",
            str(root.relative_to(root.parent)) if prefix else ".",
        )
    ]
    workdir = jobs[0].workdir
    scripts = package.get("scripts", {})
    for name, kind in (
        ("lint", "lint"),
        ("typecheck", "typecheck"),
        ("test", "test"),
        ("e2e", "e2e"),
        ("build", "build"),
    ):
        if name in scripts:
            jobs.append(Job(prefix + name, [f"{manager} run {name}"], kind, workdir))
    if (root / "tsconfig.json").exists() and "typecheck" not in scripts:
        jobs.append(Job(prefix + "typecheck", ["npx tsc --noEmit"], "typecheck", workdir))
    deps = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    if "e2e" not in scripts and (
        "@playwright/test" in deps or list(root.glob("playwright.config.*"))
    ):
        jobs.append(Job(prefix + "e2e", ["npx playwright test"], "e2e", workdir))
    return jobs


def _python(root: Path, prefix: str, workdir: str) -> list[Job]:
    path = root / "pyproject.toml"
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    jobs = []
    locked = (root / "uv.lock").exists()
    if locked:
        jobs.append(Job(prefix + "install", ["uv sync --all-extras --dev"], "setup", workdir))
    groups = data.get("dependency-groups", {})
    dependencies = data.get("project", {}).get("dependencies", []) + [
        dep for group in groups.values() if isinstance(group, list) for dep in group
    ]
    deps = " ".join(str(dep).lower() for dep in dependencies)
    tool = data.get("tool", {})
    runner = "uv run " if locked else ""
    if "ruff" in tool or "ruff" in deps:
        jobs.append(
            Job(
                prefix + "lint",
                [runner + "ruff check .", runner + "ruff format --check ."],
                "lint",
                workdir,
            )
        )
    for checker in ("mypy", "pyright"):
        if checker in tool or checker in deps:
            jobs.append(Job(prefix + "typecheck", [runner + f"{checker} ."], "typecheck", workdir))
            break
    jobs.append(Job(prefix + "test", [runner + "pytest"], "test", workdir))
    return jobs


def _detect_dir(root: Path, prefix: str, workdir: str) -> list[Job]:
    jobs = []
    jobs.extend(_node(root, prefix))
    jobs.extend(_python(root, prefix, workdir))
    if (root / "Cargo.toml").exists():
        jobs.extend(
            [
                Job(prefix + "lint", ["cargo fmt --check", "cargo clippy"], "lint", workdir),
                Job(prefix + "test", ["cargo test"], "test", workdir),
            ]
        )
    if (root / "CMakeLists.txt").exists():
        jobs.extend(
            [
                Job(
                    prefix + "build",
                    ["cmake -S . -B build", "cmake --build build"],
                    "build",
                    workdir,
                ),
                Job(
                    prefix + "test", ["ctest --test-dir build --output-on-failure"], "test", workdir
                ),
            ]
        )
    if (root / "pom.xml").exists() or (root / "mvnw").exists():
        jobs.append(
            Job(
                prefix + "test",
                ["./mvnw -B test" if (root / "mvnw").exists() else "mvn -B test"],
                "test",
                workdir,
            )
        )
    if (root / "go.mod").exists():
        jobs.extend(
            [
                Job(prefix + "lint", ["go vet ./..."], "lint", workdir),
                Job(prefix + "test", ["go test ./..."], "test", workdir),
            ]
        )
    return jobs


def detect(root: Path) -> list[Job]:
    if any((root / marker).exists() for marker in MARKERS):
        raw = _detect_dir(root, "", ".")
    else:
        raw = [
            job
            for child in sorted(root.iterdir())
            if child.is_dir() and any((child / marker).exists() for marker in MARKERS)
            for job in _detect_dir(child, child.name + ":", child.name)
        ]
    names: dict[str, int] = {}
    for job in raw:
        count = names.get(job.name, 0)
        names[job.name] = count + 1
        if count:
            job.name = f"{job.name}-{count + 1}"
    return raw
