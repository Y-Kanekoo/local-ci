# HUMAN_TODO

エージェントでは実施しない（アカウント・secret・外部連携が必要な）作業です。

- [ ] **Discord 通知を使う場合**: Discord のチャンネル設定 → 連携サービス → ウェブフックで Webhook URL を発行し、`LOCAL_CI_DISCORD_WEBHOOK_URL` をシェル（launchd で使う場合は plist の `EnvironmentVariables` か、権限 600 のファイルから読み込むラッパー）に設定する。URL はリポジトリにコミットしない。
- [ ] **定期実行**: `local-ci launchd install --command 'run-all --discord'` などで plist を生成し、表示された `launchctl bootstrap gui/$(id -u) ...` を手動実行する。
- [ ] **testcase-manager の e2e を実行したい場合**: `examples/testcase-manager.local-ci.yml` を `~/.local-ci/repos/testcase-manager.yml` にコピーする。Docker Desktop を起動し、ホストの 5432 / 8080 / 5173 番ポートを空ける（`playwright.config.ts` が `reuseExistingServer: true` のため、5173 で別プロジェクトの Vite が動いているとそれを再利用して失敗する）。必要なら `cd e2e && npx playwright install chromium`。
- [ ] **任意（testcase-manager 側の改善提案）**: `playwright.config.ts` の `reuseExistingServer` を `!process.env.CI` にする、または webServer の `stdout: "pipe"` を付けると、CI 環境での誤再利用防止と起動失敗原因（Spring Boot のログ）の表示ができる。
