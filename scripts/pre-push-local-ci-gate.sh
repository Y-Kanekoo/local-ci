#!/usr/bin/env bash
# local-ci 軽い層（stage: static, unit）の pre-push ゲート。
#
# グローバル pre-push フック（~/.git-hooks/pre-push）から、push 対象行
# （local_ref local_sha remote_ref remote_sha）を標準入力で受け取って呼ばれる。
# 2層 CI 恒久運用（docs/ci-two-tier.md）の軽い層の実装。
#
# - 対象 SHA について軽い層の成功記録（~/.local-ci/runs）があれば再実行しない
# - 軽い層に実行対象ジョブが無いリポジトリは素通りする
# - ジョブが失敗したら push を止める（exit 1）
# - 基盤側のエラー（plan の取得失敗など）では止めない（fail-open）。
#   push 後の非同期実行（watch）が commit status で結果を返すため、検出はそちらでも担保する
# - 緊急時のみ LOCAL_CI_GATE=0 で無効化できる（恒久運用ルール: 常用禁止）
set -uo pipefail

# フック経由で継承される git 環境変数が local-ci の git 操作へ漏れないようにする
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

if [ "${LOCAL_CI_GATE:-1}" = "0" ]; then
  echo "[local-ci gate] LOCAL_CI_GATE=0 のためスキップします（緊急時専用）" >&2
  exit 0
fi

command -v local-ci >/dev/null 2>&1 || exit 0
command -v git >/dev/null 2>&1 || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

# push 対象から、削除でない最後のブランチ更新 SHA を選ぶ
target_sha=""
while read -r local_ref local_sha remote_ref remote_sha; do
  [ -z "${local_sha:-}" ] && continue
  [ "$local_sha" = "0000000000000000000000000000000000000000" ] && continue
  case "$local_ref" in
    refs/heads/*) target_sha="$local_sha" ;;
  esac
done
[ -z "$target_sha" ] && exit 0

# 対象リポジトリのルート（worktree から push しても本体リポジトリ側で判定する）
top="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
common="$(git rev-parse --git-common-dir 2>/dev/null)" || exit 0
case "$common" in
  /*) root="$(cd "$common/.." 2>/dev/null && pwd -P)" ;;
  *)  root="$(cd "$top/$common/.." 2>/dev/null && pwd -P)" ;;
esac
[ -n "${root:-}" ] || root="$top"
name="$(basename "$root")"
runs_dir="${LOCAL_CI_HOME:-$HOME/.local-ci}/runs/$name"

# 高速パス: 同一 SHA の成功記録があり、軽い層のジョブが含まれていれば再実行しない
if python3 - "$runs_dir" "$target_sha" <<'PY'
import json
import sys
from pathlib import Path

runs = Path(sys.argv[1])
sha = sys.argv[2]
if not runs.is_dir():
    sys.exit(1)
for run_dir in sorted(runs.iterdir(), reverse=True):
    result_file = run_dir / "result.json"
    if not result_file.is_file():
        continue
    try:
        result = json.loads(result_file.read_text(encoding="utf-8"))
    except Exception:
        continue
    if result.get("sha") != sha or result.get("status") != "success":
        continue
    stages = {job.get("stage") for job in result.get("jobs", [])}
    if stages & {"static", "unit"}:
        sys.exit(0)
sys.exit(1)
PY
then
  echo "[local-ci gate] $name の軽い層は成功記録があるためスキップします ($target_sha)" >&2
  exit 0
fi

# 軽い層に実行対象ジョブが無ければ素通りする
plan_json="$(local-ci plan "$root" --ref "$target_sha" --stage static,unit --json 2>/dev/null)" || {
  echo "[local-ci gate] 警告: plan を取得できないためゲートをスキップします" >&2
  exit 0
}
if ! python3 -c '
import json
import sys

try:
    plan = json.load(sys.stdin)
except Exception:
    sys.exit(1)
jobs = [
    job
    for job in plan.get("jobs", [])
    if not job.get("skip") and job.get("stage") in ("static", "unit")
]
sys.exit(0 if jobs else 1)
' <<<"$plan_json"; then
  exit 0
fi

echo "[local-ci gate] $name の軽い層（static, unit）を実行します: $target_sha" >&2
if local-ci run "$root" --ref "$target_sha" --stage static,unit; then
  echo "[local-ci gate] 軽い層 OK" >&2
  exit 0
fi
echo "[local-ci gate] 軽い層が失敗したため push を止めました。詳細: local-ci logs $name" >&2
echo "[local-ci gate] 重い層（api, e2e）は push 後に非同期で実行されます。" >&2
echo "[local-ci gate] 緊急時のみ LOCAL_CI_GATE=0 git push が使えます（常用禁止）" >&2
exit 1
