# local-ci 作業規約

- Python 3.11+、src レイアウト `src/local_ci/`。依存関係は `pyproject.toml` と `uv.lock` で管理する。
- 変更後は `uv run ruff check .`、`uv run ruff format --check .`、`uv run pytest -q` を実行する。
- ユーザーの作業ツリーに対して stash / checkout / reset / clean を実行しない。実行には必ず自分で作成した専用 worktree を使い、それだけを削除する。
- ログ、結果、GitHub 報告の内容は保存・送信前にマスクする。`GH_TOKEN` と `GITHUB_TOKEN` は明示許可なしでジョブに渡さない。
- テストはネットワークを使わず、一時ディレクトリで git リポジトリを作る。
- `plan` と `run` は指定 ref の専用 worktree で設定を読み、リポジトリの `.local-ci.yml`、`$LOCAL_CI_HOME/repos/<repo名>.yml`、自動検出の順に選ぶ。
- ジョブは既定で setup → lint → typecheck → test → build → e2e に安定ソートする。`order: config` のときのみ設定順を維持する。
- `before` の失敗は以降のジョブをスキップして全体を failure にし、`after` は成功・失敗・中断時に実行する。フックのログと結果もマスクして保存する。
- 子プロセスから親の仮想環境・Conda・uv 変数と npm メタデータ変数を除去し、親の `VIRTUAL_ENV/bin` を PATH から除く。Playwright e2e には `CI=1` と `PLAYWRIGHT_FORCE_TTY=0` を渡す。
- 集計は `notify.py` が担当し、ファイル出力と Discord Webhook 送信（標準ライブラリの urllib のみ）を行う。Webhook URL は環境変数からのみ読み、保存・ログ出力しない。テストでは送信関数を差し替え、ネットワークを使わない。
- `compose.yaml` 等があるリポジトリの自動検出 e2e は `needs_services` として理由付きでスキップする。
