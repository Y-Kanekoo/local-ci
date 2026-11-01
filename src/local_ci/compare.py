"""2つのコミットを同じ設定・段階ごとに実行して比べる（修正の前後比較）。

適用前のコミットに .local-ci.yml が無いと自動検出の設定で動き、ジョブ構成が変わって
比べられない。また fail_fast で後段が skipped になると、後段の回帰が隠れる。
そこで compare は次の決まりで実行する。

- 設定は1つに固定し、両方のコミットに同じものを当てる。
  優先順: --config で指定したファイル → head コミットの .local-ci.yml →
  LOCAL_CI_HOME の repos/<name>.yml → （どれも無ければ）各コミットの自動検出。
- fail_fast は常に無効にして、全段階を実行する。
- ジョブ名で突き合わせ、回帰（base で成功 → head で失敗）があれば終了コード 1。
"""

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from local_ci.config import STAGE_ORDER
from local_ci.notify import FAILED
from local_ci.runner import git, home, repo_name, run

RunFunc = Callable[..., dict[str, Any]]

# ジョブ単位の判定。並び順は表示の優先度（回帰を先頭に出す）。
CATEGORIES = (
    "regression",
    "new_failure",
    "still_failing",
    "fixed",
    "removed",
    "unchanged",
)
LABELS = {
    "regression": "回帰（base 成功 → head 失敗）",
    "new_failure": "head だけにあるジョブの失敗（回帰とみなす）",
    "still_failing": "もともと失敗（両方で失敗）",
    "fixed": "修正（base 失敗 → head 成功）",
    "removed": "base だけにあるジョブ",
    "unchanged": "変化なし",
}


def _failed(job: dict[str, Any] | None) -> bool:
    if not job:
        return False
    return job.get("status") in FAILED and not job.get("allow_failure")


def classify(base: dict[str, Any] | None, head: dict[str, Any] | None) -> str:
    """1つのジョブの base と head の結果から判定を返す。"""
    if head is None:
        return "removed"
    if base is None:
        return "new_failure" if _failed(head) else "unchanged"
    base_failed, head_failed = _failed(base), _failed(head)
    if head_failed and base_failed:
        return "still_failing"
    if head_failed:
        return "regression"
    if base_failed:
        return "fixed"
    return "unchanged"


def _stage_of(job: dict[str, Any]) -> str:
    return str(job.get("stage") or "unknown")


def build_comparison(
    base_result: dict[str, Any], head_result: dict[str, Any], config_label: str
) -> dict[str, Any]:
    """2つの run の結果をジョブ名で突き合わせる。"""
    base_jobs = {str(job["name"]): job for job in base_result.get("jobs") or []}
    head_jobs = {str(job["name"]): job for job in head_result.get("jobs") or []}
    names = list(head_jobs) + [name for name in base_jobs if name not in head_jobs]
    rows = []
    for name in names:
        base, head = base_jobs.get(name), head_jobs.get(name)
        source = head or base or {}
        rows.append(
            {
                "name": name,
                "stage": _stage_of(source),
                "kind": source.get("kind"),
                "base": base.get("status") if base else None,
                "head": head.get("status") if head else None,
                "category": classify(base, head),
            }
        )
    rows.sort(
        key=lambda row: (
            STAGE_ORDER.get(str(row["stage"]), len(STAGE_ORDER)),
            names.index(str(row["name"])),
        )
    )
    stages = []
    for stage in dict.fromkeys(str(row["stage"]) for row in rows):
        stage_rows = [row for row in rows if row["stage"] == stage]
        counts = {category: 0 for category in CATEGORIES}
        for row in stage_rows:
            counts[str(row["category"])] += 1
        stages.append({"stage": stage, "jobs": len(stage_rows), **counts})
    totals = {
        category: sum(1 for row in rows if row["category"] == category) for category in CATEGORIES
    }
    regressed = totals["regression"] + totals["new_failure"]
    warnings = []
    base_source = base_result.get("config_source")
    head_source = head_result.get("config_source")
    if base_source != head_source:
        warnings.append(
            f"base と head で設定の出所が違います（base: {base_source}, head: {head_source}）。"
            "--config で同じ設定を指定してください"
        )
    for label, result in (("base", base_result), ("head", head_result)):
        if result.get("status") in {"error", "interrupted"}:
            warnings.append(f"{label} の実行が完了していません（{result.get('status')}）")
    return {
        "repo": head_result.get("repo") or base_result.get("repo"),
        "config": config_label,
        "base": {
            "ref": base_result.get("ref"),
            "sha": base_result.get("sha"),
            "status": base_result.get("status"),
            "run_dir": base_result.get("run_dir"),
        },
        "head": {
            "ref": head_result.get("ref"),
            "sha": head_result.get("sha"),
            "status": head_result.get("status"),
            "run_dir": head_result.get("run_dir"),
        },
        "status": "regression" if regressed else "ok",
        "totals": totals,
        "stages": stages,
        "jobs": rows,
        "warnings": warnings,
    }


def render_markdown(comparison: dict[str, Any]) -> str:
    base = comparison["base"]
    head = comparison["head"]
    lines = [
        f"# local-ci compare: {comparison['repo']}",
        "",
        f"- base: `{base['ref']}` (`{str(base['sha'])[:7]}`) → {base['status']}",
        f"- head: `{head['ref']}` (`{str(head['sha'])[:7]}`) → {head['status']}",
        f"- 設定: `{comparison['config']}`（両方に同じ設定、fail_fast は無効）",
        f"- 判定: **{comparison['status']}**",
        "",
    ]
    for warning in comparison["warnings"]:
        lines.append(f"> 警告: {warning}")
    if comparison["warnings"]:
        lines.append("")
    lines += [
        "| stage | jobs | 回帰 | head のみ失敗 | もともと失敗 | 修正 | base のみ | 変化なし |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for entry in comparison["stages"]:
        lines.append(
            f"| {entry['stage']} | {entry['jobs']} | {entry['regression']} | "
            f"{entry['new_failure']} | {entry['still_failing']} | {entry['fixed']} | "
            f"{entry['removed']} | {entry['unchanged']} |"
        )
    lines += ["", "| job | stage | base | head | 判定 |", "| --- | --- | --- | --- | --- |"]
    for row in comparison["jobs"]:
        lines.append(
            f"| {row['name']} | {row['stage']} | {row['base'] or '-'} | {row['head'] or '-'} | "
            f"{LABELS[str(row['category'])]} |"
        )
    return "\n".join(lines) + "\n"


def _head_config(repo: Path, head: str, directory: Path) -> Path | None:
    try:
        content = git(repo, "show", f"{head}:.local-ci.yml")
    except subprocess.CalledProcessError:
        return None
    path = directory / ".local-ci.yml"
    path.write_text(content + "\n", encoding="utf-8")
    return path


def compare(
    repo: Path,
    base: str,
    head: str = "HEAD",
    config_path: Path | None = None,
    stages: set[str] | None = None,
    quiet: bool = False,
    runner: RunFunc = run,
) -> dict[str, Any]:
    """base と head を同じ設定・fail_fast 無効で実行し、比較結果を返す。"""
    repo = Path(git(repo.expanduser().resolve(), "rev-parse", "--show-toplevel"))
    with tempfile.TemporaryDirectory(prefix="local-ci-compare-") as temp:
        label = "自動検出（各コミット）"
        chosen: Path | None = None
        if config_path is not None:
            chosen = config_path.expanduser().resolve()
            if not chosen.is_file():
                raise ValueError(f"設定ファイルが見つかりません: {config_path}")
            label = str(config_path)
        else:
            from_head = _head_config(repo, head, Path(temp))
            external = home() / "repos" / f"{repo_name(repo)}.yml"
            if from_head is not None:
                chosen, label = from_head, f"{head}:.local-ci.yml"
            elif external.is_file():
                chosen, label = external, f"LOCAL_CI_HOME/repos/{repo_name(repo)}.yml"
        results = []
        for ref in (base, head):
            results.append(
                runner(
                    repo,
                    ref=ref,
                    quiet=quiet,
                    stages=stages,
                    fail_fast=False,
                    config_path=chosen,
                )
            )
    return build_comparison(results[0], results[1], label)


def write_output(path: Path, comparison: dict[str, Any]) -> Path:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".json":
        path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", "utf-8")
    else:
        path.write_text(render_markdown(comparison), encoding="utf-8")
    return path
