# local-ci 作業規約

- Python 3.11+、src レイアウト `src/local_ci/`。依存関係は `pyproject.toml` と `uv.lock` で管理する。
- 変更後は `uv run ruff check .`、`uv run ruff format --check .`、`uv run pytest -q` を実行する。
- ユーザーの作業ツリーに対して stash / checkout / reset / clean を実行しない。実行には必ず自分で作成した専用 worktree を使い、それだけを削除する。
- ログ、結果、GitHub 報告の内容は保存・送信前にマスクする。`GH_TOKEN` と `GITHUB_TOKEN` は明示許可なしでジョブに渡さない。
- テストはネットワークを使わず、一時ディレクトリで git リポジトリを作る。
