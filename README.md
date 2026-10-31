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
local-ci logs my-app
```

対象リポジトリのコミットに `.local-ci.yml` があれば使用し、なければファイル構成からジョブを検出します。`--only` / `--skip` は `kind` のカンマ区切りです。`--keep-worktree` で作成した worktree を残せます。`LOCAL_CI_HOME` で保存先（既定 `~/.local-ci`）を変更できます。

## 設定ファイル

リポジトリ直下の `.local-ci.yml` をコミットしてください。`run` はコマンド文字列または文字列の配列で、シェル演算子を使わず引数として実行します。`workdir` は worktree 内の相対パスです。`env` の値は `${ENV_VAR}` 形式のみ許可します。秘密値はファイルに直書きしないでください。

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

`kind` は `lint` / `typecheck` / `test` / `e2e` / `build` / `setup`。`timeout` の既定は 1800 秒です。`setup` は選択されたジョブに共通の前処理です。`needs_services: true` のジョブは外部サービスを自動起動せず `skipped` にします。サービスを手動起動して実行する場合はこの値を外し、必要に応じて `allow_failure: true` を設定します。`skip: true` は明示的なスキップです。`include_auto: true` は名前が重複しない自動検出ジョブを追加します。さらに [examples](examples) を参照してください。

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

ルートに上記ファイルがなければ直下 1 階層のディレクトリを検出し、`frontend:lint` のように命名します。PATH にないツールのジョブは理由をログに書いて `skipped` とします。Node 依存は各 worktree 内へ新規導入し、パッケージマネージャーのキャッシュを利用します。

Playwright の Chromium は自動導入しません。未導入なら、対象プロジェクトで `npx playwright install chromium` を実行するか、設定の `setup` に明記してください。

## 結果と GitHub 報告

ログ、`result.json`、`summary.md` は `~/.local-ci/runs/<repo>/<日時>-<sha7>/` に保存します。`local-ci logs <repo>` は最新の summary を表示します。`--report status` は `local-ci/<ジョブ名>` と `local-ci/overall` の commit status を `gh api` で送信します。`--report comment` は PR にサマリと失敗ログ末尾 40 行をコメントし、同じマーカーのコメントがあれば編集します。既定は `--report none` です。`--dry-run-report` は送信内容を表示します。gh の失敗はジョブ結果を変えません。

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

秘密を示す名前の環境変数（値 6 文字以上）と既知のトークン、秘密鍵、Basic 認証 URL、Bearer ヘッダーを保存前にマスクします。ジョブには `GH_TOKEN` と `GITHUB_TOKEN` を既定で渡しません。必要な場合だけジョブの `pass_env` に明示します。マスクは未知形式の秘密を完全には検出できないため、CI 設定と実行コマンドに秘密値を直書きしないでください。

ジョブはローカルの権限で動くため、信頼できるリポジトリと設定だけ実行してください。コマンドはシェル経由で起動せず、基本的な破壊的コマンドを拒否しますが、任意のプログラム内部の副作用までは制限しません。外部サービス、ブラウザ、言語ツールは自動インストールしません。対象の ref がローカルになければ `git fetch origin` を試みるため、その場合だけネットワークが必要です。
