"""設定読み込みとリポジトリの自動検出。"""

import json
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

KINDS = {"lint", "typecheck", "test", "api", "e2e", "build", "setup"}
KIND_ORDER = {
    kind: index
    for index, kind in enumerate(("setup", "lint", "typecheck", "test", "build", "api", "e2e"))
}
# テスト標準の4段階（static → unit → api → e2e）。setup は常に最初に実行する。
STAGES = ("static", "unit", "api", "e2e")
STAGE_ORDER = {stage: index for index, stage in enumerate(("setup", *STAGES))}
KIND_STAGE = {
    "setup": "setup",
    "lint": "static",
    "typecheck": "static",
    "test": "unit",
    "build": "unit",
    "api": "api",
    "e2e": "e2e",
}
MARKERS = {
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "CMakeLists.txt",
    "pom.xml",
    "mvnw",
    "go.mod",
}
COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
SERVICE_SKIP_REASON = (
    "{compose} があり DB 等の外部サービスが必要な可能性があるため、自動検出した e2e を"
    "スキップしました。before フックでサービスを起動する設定を .local-ci.yml または "
    "$LOCAL_CI_HOME/repos/<repo名>.yml に書くと実行できます"
)
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
    skip_reason: str | None = None
    stage: str = ""

    def __post_init__(self) -> None:
        if not self.stage:
            self.stage = KIND_STAGE.get(self.kind, self.kind)


def job_order(job: Job) -> tuple[int, int]:
    """ステージ順、同じステージ内は kind 順（従来の kind 順と同じ並びになる）。"""
    return STAGE_ORDER.get(job.stage, len(STAGE_ORDER)), KIND_ORDER.get(job.kind, len(KIND_ORDER))


def parse_stages(value: str | None) -> set[str] | None:
    """`--stage static,unit` を検証して集合にする。"""
    if not value:
        return None
    stages = {item.strip() for item in value.split(",") if item.strip()}
    unknown = stages - set(STAGES)
    if not stages or unknown:
        raise ValueError(
            f"--stage には {', '.join(STAGES)} をカンマ区切りで指定してください: "
            f"{', '.join(sorted(unknown)) or value}"
        )
    return stages


@dataclass
class Config:
    jobs: list[Job]
    setup: list[str] = field(default_factory=list)
    before: list[Job] = field(default_factory=list)
    after: list[Job] = field(default_factory=list)
    source: str = "auto"
    fail_fast: bool = False


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


def _job(name: str, raw: object, hook_kind: str | None = None) -> Job:
    if not isinstance(raw, dict):
        raise ValueError(f"ジョブ {name} はマッピングで指定してください")
    kind = hook_kind or raw.get("kind", name.split(":")[-1])
    if hook_kind is None and kind not in KINDS:
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
    stage = raw.get("stage", "") if hook_kind is None else ""
    if stage and (kind == "setup" or stage not in STAGES):
        raise ValueError(
            f"ジョブ {name} の stage は {', '.join(STAGES)} のいずれかで指定してください"
            "（kind: setup には指定できません）"
        )
    skip_reason = raw.get("skip_reason") if hook_kind is None else None
    if skip_reason is not None and (
        not isinstance(skip_reason, str) or not skip_reason.strip() or not raw.get("skip", False)
    ):
        raise ValueError(
            f"ジョブ {name} の skip_reason は skip: true と一緒に文字列で指定してください"
        )
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
        skip_reason.strip() if skip_reason else None,
        stage,
    )


def _hooks(raw: object, phase: str) -> list[Job]:
    if not isinstance(raw, list):
        raise ValueError(f"{phase} はフックの配列で指定してください")
    hooks = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise ValueError(f"{phase} の各要素には name が必要です")
        if set(item) - {"name", "run", "workdir", "env", "timeout"}:
            raise ValueError(f"{phase} の要素に使用できないキーがあります: {item['name']}")
        hooks.append(_job(item["name"], item, phase))
    if len({hook.name for hook in hooks}) != len(hooks):
        raise ValueError(f"{phase} の name は重複できません")
    return hooks


def load_config(root: Path, external_path: Path | None = None) -> Config:
    path = root / ".local-ci.yml"
    if not path.exists():
        if external_path is None or not external_path.exists():
            return Config(sorted(detect(root), key=job_order))
        path = external_path
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict) or not isinstance(raw.get("jobs", {}), dict):
        raise ValueError(f"{path} の jobs はマッピングで指定してください")
    jobs = [_job(name, value) for name, value in raw.get("jobs", {}).items()]
    if raw.get("include_auto", False):
        existing = {job.name for job in jobs}
        jobs.extend(job for job in detect(root) if job.name not in existing)
    setup = _commands(raw["setup"]) if "setup" in raw else []
    order = raw.get("order", "kind")
    if order not in {"kind", "config"}:
        raise ValueError("order は kind または config を指定してください")
    if order != "config":
        jobs.sort(key=job_order)
    fail_fast = raw.get("fail_fast", False)
    if not isinstance(fail_fast, bool):
        raise ValueError("fail_fast は true または false で指定してください")
    source = ".local-ci.yml" if path == root / ".local-ci.yml" else str(path)
    return Config(
        jobs,
        setup,
        _hooks(raw.get("before", []), "before"),
        _hooks(raw.get("after", []), "after"),
        source,
        fail_fast,
    )


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
    # test:unit と test:api が両方ある場合だけ、test の代わりに段階別のスクリプトを使う
    # （test が両方を含むことが多く、二重実行を避けるため）。
    split = "test:unit" in scripts and "test:api" in scripts
    for name, kind, script in (
        ("lint", "lint", "lint"),
        ("typecheck", "typecheck", "typecheck"),
        ("test", "test", "test:unit" if split else "test"),
        ("build", "build", "build"),
        ("api", "api", "test:api"),
        ("e2e", "e2e", "e2e"),
    ):
        if script in scripts:
            jobs.append(Job(prefix + name, [f"{manager} run {script}"], kind, workdir))
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
    if (root / "tests" / "api").is_dir():
        # tests/api はAPI段階として分け、unit段階では除外する（全テストは両段階で一度ずつ実行）。
        jobs.append(Job(prefix + "test", [runner + "pytest --ignore=tests/api"], "test", workdir))
        jobs.append(Job(prefix + "api", [runner + "pytest tests/api"], "api", workdir))
    else:
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
    root_compose = next((name for name in COMPOSE_FILES if (root / name).exists()), None)
    for job in raw:
        if job.kind != "e2e":
            continue
        compose = root_compose or next(
            (
                f"{job.workdir}/{name}"
                for name in COMPOSE_FILES
                if (root / job.workdir / name).exists()
            ),
            None,
        )
        if compose:
            job.needs_services = True
            job.skip_reason = SERVICE_SKIP_REASON.format(compose=compose)
    names: dict[str, int] = {}
    for job in raw:
        count = names.get(job.name, 0)
        names[job.name] = count + 1
        if count:
            job.name = f"{job.name}-{count + 1}"
    return raw
