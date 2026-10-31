# local-ci

GitHub Actions の実行枠に依存せず、macOS 上の専用 git worktree で CI ジョブを実行します。既定では現在の `HEAD` コミットを対象にし、未コミットの変更は含めません。実行後に作成した worktree を削除し、作業中のチェックアウトのファイル、HEAD、index を変更しません。

## インストール

Python 3.11 以上、`uv`、`git` が必要です。GitHub 報告と PR 監視には `gh` が必要です。

```sh
uv tool install git+https://github.com/Y-Kanekoo/local-ci
# またはクローンしたディレクトリで
uv tool install .
```

## クイックスタート

```sh
local-ci run ~/Projects/my-app
local-ci run ~/Projects/my-app --ref main --only lint,test
local-ci run ~/Projects/my-app --pr 42 --report comment
local-ci plan ~/Projects/my-app --ref main
local-ci plan ~/Projects/my-app --json
local-ci logs my-app
```

設定の優先順位は、対象コミットの `.local-ci.yml`、`$LOCAL_CI_HOME/repos/<repo名>.yml`、自動検出です。外部設定は対象リポジトリへコミットせずに使えます。`config_source` を `result.json`、`summary.md`、`plan` に記録します。`--only` / `--skip` は `kind` のカンマ区切りです。`--keep-worktree` で作成した worktree を残せます。`LOCAL_CI_HOME` で保存先（既定 `~/.local-ci`）を変更できます。

`run` は各ジョブ・フックの開始と終了を stderr に表示します。`--quiet` で抑制できます。`run-all` では行の先頭にリポジトリ名を表示します。`plan` は指定 ref の一時 worktree で設定とファイルを読み、ジョブの名前・kind・workdir・コマンド・スキップ予定と理由を表示してから worktree を削除します。`--json` も利用できます。

## 設定ファイル

リポジトリ直下の `.local-ci.yml`、またはローカル専用の `$LOCAL_CI_HOME/repos/<repo名>.yml` に設定します。`run` はコマンド文字列または文字列の配列で、シェル演算子を使わず引数として実行します。`workdir` は worktree 内の相対パスです。`env` の値は `${ENV_VAR}` 形式のみ許可します。秘密値はファイルに直書きしないでください。

```yaml
setup:
  - uv sync --all-extras --dev
jobs:
  lint:
    kind: lint
    run: ["uv run ruff check .", "uv run ruff format --check ."]
    timeout: 1800
  test:
    kind: test
    run: uv run pytest
    workdir: .
    env:
      DATABASE_URL: ${TEST_DATABASE_URL}
  browser:
    kind: e2e
    run: npx playwright test
    needs_services: true
    skip: true
    allow_failure: true
include_auto: false
```

`kind` は `setup` / `lint` / `typecheck` / `test` / `build` / `e2e`。この順に安定ソートし、モノレポでも全 setup を先に実行します。設定に書いた順を維持する場合だけ `order: config` を指定します。`timeout` の既定は 1800 秒です。`setup` は選択されたジョブに共通の前処理です。`needs_services: true` のジョブは外部サービスが必要なため `skipped` にします。サービスをフックで起動して実行する場合はこの値を外します。`skip: true` は明示的なスキップです。`allow_failure: true` の失敗は `failed (allowed)` と表示し、全体の失敗には含めません。`include_auto: true` は名前が重複しない自動検出ジョブを追加します。さらに [examples](examples) を参照してください。

### サービスの起動と停止

`before` はジョブの前に実行します。失敗した場合、以降のジョブをスキップし、全体を failure にします。`after` は成功・失敗・Ctrl-C のいずれでも実行し、その結果は全体ステータスに影響しません。各要素に `name`、`run`、`workdir`、`env`、`timeout` を指定できます。`run` の `{repo}`、`{sha}`、`{short_sha}`、`{worktree}` は実行時に展開します。フックの結果とログも保存します。

```yaml
before:
  - name: postgres-start
    run: docker compose -p local-ci-{repo} up -d postgres --wait
    timeout: 120
after:
  - name: postgres-stop
    run: docker compose -p local-ci-{repo} down
jobs:
  browser:
    kind: e2e
    workdir: frontend
    run: npx playwright test
```

Playwright の e2e ジョブには `CI=1` と `PLAYWRIGHT_FORCE_TTY=0` を付与します。失敗ログに `config.webServer` がある場合、依存サービスを `before` で起動するヒントを summary に表示します。

### 自動検出

| ファイル | ジョブ |
|---|---|
| `package.json` | ロックファイルに応じた npm / pnpm / yarn install、存在する lint / typecheck / test / e2e / build scripts |
| `tsconfig.json` | typecheck script がなければ `npx tsc --noEmit` |
| `@playwright/test` または `playwright.config.*` | e2e script がなければ `npx playwright test` |
| `pyproject.toml` | `uv.lock` があれば `uv sync --all-extras --dev`、ruff があれば check と format、mypy / pyright があれば型検査、pytest |
| `Cargo.toml` | `cargo fmt --check`、`cargo clippy`、`cargo test` |
| `CMakeLists.txt` | cmake configure / build、ctest |
| `pom.xml` / `mvnw` | Maven test |
| `go.mod` | `go vet ./...`、`go test ./...` |

リポジトリ直下（または e2e のディレクトリ）に `compose.yaml` / `docker-compose.yml` などがある場合、自動検出した e2e は DB 等の外部サービスが必要とみなして理由付きで `skipped` にします。`before` フックでサービスを起動する設定を書けば実行できます（例: [examples/testcase-manager.local-ci.yml](examples/testcase-manager.local-ci.yml)）。明示設定した e2e はこの判定の対象外です。

ルートに上記ファイルがなければ直下 1 階層のディレクトリを検出し、`frontend:lint` のように命名します。PATH にないツールのジョブは理由をログに書いて `skipped` とします。ログ名では `:` や `/` を `__` に置換し、結果中のジョブ名は維持します。Node 依存は各 worktree 内へ新規導入し、パッケージマネージャーのキャッシュを利用します。

Playwright の Chromium は自動導入しません。未導入なら、対象プロジェクトで `npx playwright install chromium` を実行するか、設定の `setup` に明記してください。

## 結果と GitHub 報告

ログ、`result.json`、`summary.md` は `~/.local-ci/runs/<repo>/<日時>-<sha7>/` に保存します。`local-ci logs <repo>` は最新の summary を表示します。`--report status` は `local-ci/<ジョブ名>` と `local-ci/overall` の commit status を `gh api` で送信します。`--report comment` は PR にサマリと失敗ログ末尾 40 行をコメントし、同じマーカーのコメントがあれば編集します。既定は `--report none` です。`--dry-run-report` は送信内容を表示します。gh の失敗はジョブ結果を変えません。

## 集計の出力（ファイル / Discord）

`run-all` と `watch` は実行ごとに全リポジトリの結果を集計し、`$LOCAL_CI_HOME/summary/latest.md` と `latest.json` に保存して、Markdown 表を標準出力に表示します。`--output <path>` で任意の場所にも書き出します（拡張子 `.json` なら JSON、それ以外は Markdown）。`run` でも `--output` / `--discord` を使えます。

```sh
local-ci run-all --root ~/Projects --output ~/ci-summary.md
export LOCAL_CI_DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...'
local-ci run-all --root ~/Projects --discord
```

`--discord` は環境変数 `LOCAL_CI_DISCORD_WEBHOOK_URL`（グローバル設定の `discord_webhook_env` で変数名を変更可）の Discord Webhook へ、リポジトリごとの結果と失敗ジョブ名を 2000 文字以内で送信します。URL は `https://discord.com/api/webhooks/` 形式のみ受け付け、メンションは無効化します。URL はファイルにもログにも保存せず、`WEBHOOK` を含む環境変数はジョブへ渡しません（必要なら `pass_env`）。未設定や送信失敗は警告のみで、終了コードは CI 結果で決まります。有料 API は使いません。グローバル設定では `output: ~/ci-summary.md`、`discord: true` も指定できます。

## 複数リポジトリと監視

```sh
local-ci run-all --root ~/Projects --include 'app-*' --exclude 'app-old' --jobs 2
local-ci watch --once --root ~/Projects --branches main --report status
```

`run-all` は root 直下の `.git` **ディレクトリ**を持つメインチェックアウトを対象とし、`.git` ファイルの worktree は除外します。`watch` は `gh pr list` の PR head SHA と指定ブランチの新コミットを `~/.local-ci/state.json` で記録します。未実行の組だけ実行します。継続監視時の間隔は `--interval` 秒（既定 900）。グローバル設定 `~/.local-ci/config.yml` の例:

```yaml
root: ~/Projects
include: ["app-*"]
exclude: ["app-old"]
jobs: 2
report: status
watch_repos: ["~/Projects/app-api"]
branches: [main]
```

## 定期実行

```sh
local-ci launchd install --interval 900 --command 'watch --once'
local-ci launchd install --print
local-ci launchd uninstall
local-ci cron print
```

`launchd install` は `~/Library/LaunchAgents/io.github.y-kanekoo.local-ci.plist` の生成だけを行い、PATH を明示します。表示された `launchctl bootstrap gui/$(id -u) <plist>` をユーザーが手で実行して有効化してください。停止は表示された `launchctl bootout ...` です。`uninstall` は bootout 手順を表示してから plist を削除します。`cron print` は crontab 行を表示するだけです。

## 秘密情報と制限

秘密を示す名前の環境変数（値 6 文字以上）と既知のトークン、秘密鍵、Basic 認証 URL、Bearer ヘッダーを保存前にマスクします。ジョブには `GH_TOKEN` と `GITHUB_TOKEN` を既定で渡しません。必要な場合だけジョブの `pass_env` に明示します。親プロセスの Python 仮想環境・Conda・uv 環境変数と npm の `npm_config_*` / `npm_lifecycle_*` / `npm_package_*` を子プロセスから除外し、親の `VIRTUAL_ENV/bin` も PATH から外します。マスクは未知形式の秘密を完全には検出できないため、CI 設定と実行コマンドに秘密値を直書きしないでください。

ジョブはローカルの権限で動くため、信頼できるリポジトリと設定だけ実行してください。コマンドはシェル経由で起動せず、基本的な破壊的コマンドを拒否しますが、任意のプログラム内部の副作用までは制限しません。外部サービス、ブラウザ、言語ツールは自動インストールしません。対象の ref がローカルになければ `git fetch origin` を試みるため、その場合だけネットワークが必要です。
