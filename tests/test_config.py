import json
from pathlib import Path

import pytest

from local_ci.config import detect, load_config


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (
            {
                "package.json": json.dumps({"scripts": {"lint": "eslint .", "test": "vitest"}}),
                "package-lock.json": "",
                "tsconfig.json": "{}",
            },
            {"install", "lint", "typecheck", "test"},
        ),
        (
            {"package.json": json.dumps({"devDependencies": {"@playwright/test": "1"}})},
            {"install", "e2e"},
        ),
        ({"pyproject.toml": '[project]\nname="x"\nversion="0.1"\n[tool.ruff]\n'}, {"lint", "test"}),
        ({"Cargo.toml": "[package]\nname='x'"}, {"lint", "test"}),
        ({"CMakeLists.txt": "project(x)"}, {"build", "test"}),
        ({"pom.xml": "<project />"}, {"test"}),
        ({"go.mod": "module example.com/x"}, {"lint", "test"}),
    ],
)
def test_detect_languages(tmp_path: Path, files: dict[str, str], expected: set[str]) -> None:
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    jobs = detect(tmp_path)
    assert {job.name for job in jobs} == expected


def test_monorepo_and_config(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"scripts":{"lint":"eslint ."}}')
    assert {job.name for job in detect(tmp_path)} == {"frontend:install", "frontend:lint"}
    (tmp_path / ".local-ci.yml").write_text(
        "include_auto: true\n"
        "setup: echo ready\n"
        "jobs:\n  lint:\n    kind: lint\n    run: echo hello\n"
        "    env:\n      API_TOKEN: ${CI_TOKEN}\n"
    )
    config = load_config(tmp_path)
    assert config.setup == ["echo ready"]
    assert {job.name for job in config.jobs} == {"lint", "frontend:install", "frontend:lint"}


@pytest.mark.parametrize(
    ("lockfile", "install"),
    [
        ("package-lock.json", "npm ci"),
        ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
        ("yarn.lock", "yarn install --frozen-lockfile"),
    ],
)
def test_node_package_manager(tmp_path: Path, lockfile: str, install: str) -> None:
    (tmp_path / "package.json").write_text("{}")
    (tmp_path / lockfile).write_text("")
    assert detect(tmp_path)[0].run == [install]


@pytest.mark.parametrize("value", ["plain-secret", "${BAD-NAME}", "${FOO}suffix"])
def test_reject_literal_env(tmp_path: Path, value: str) -> None:
    (tmp_path / ".local-ci.yml").write_text(
        f"jobs:\n  test:\n    run: echo ok\n    env:\n      TOKEN: '{value}'\n"
    )
    with pytest.raises(ValueError, match="env"):
        load_config(tmp_path)


def test_reject_unsafe_workdir(tmp_path: Path) -> None:
    (tmp_path / ".local-ci.yml").write_text(
        "jobs:\n  test:\n    run: echo ok\n    workdir: ../outside\n"
    )
    with pytest.raises(ValueError, match="workdir"):
        load_config(tmp_path)
