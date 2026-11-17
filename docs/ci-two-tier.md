# 2層 CI の恒久運用

GitHub Actions の実行枠に依存せず、自作 local-ci を検証の正ゲートとする恒久運用ルール（2026-11-17 確定）。一時的な節約策ではなく、今後の全リポジトリに適用する標準構成とする。

## 層の定義

| 層 | 対象 | 実行タイミング | 失敗時の扱い |
|---|---|---|---|
| 軽い層 | stage `static`（lint・型検査）と `unit`（高速ユニットテスト・ビルド） | pre-push フックで**同期実行** | **push を止める** |
| 重い層 | stage `api`・`e2e`、長時間テスト、証跡系ジョブ | push 後、Mac 上で **非同期実行**（watch / launchd） | push は止めない。結果を commit status（`local-ci/overall`）へ返し、失敗は検出できる状態にする |

- stage の既定対応は kind から決まる（lint/typecheck → static、test/build → unit、api → api、e2e → e2e）。ジョブごとに `stage:` を書けば上書きできる。
- **重い層への振り分けは実行時間の実測で判定する**。`~/.local-ci/runs/<repo>/…/result.json` の `duration` を見て、目安として数分を超えるジョブは `stage: api`（または `e2e`）へ寄せ、軽い層に残さない。pre-push ゲートが遅いと開発が止まるため、軽い層は短時間で終わる状態を保つ。

## 軽い層: pre-push 同期ゲート

- 実装は `scripts/pre-push-local-ci-gate.sh`。グローバルフック `~/.git-hooks/pre-push`（シークレット走査）に続けて呼ばれる。導入時はこのスクリプトを `~/.git-hooks/local-ci-gate.sh` へコピーし、pre-push から呼ぶ形にする（正本はこのリポジトリ側）。
- 対象 SHA について軽い層の成功記録（`~/.local-ci/runs`）があれば再実行しない（高速パス）。
- 軽い層に実行対象ジョブが無いリポジトリ（自動検出でもジョブが無いもの）は素通りする。
- 基盤側のエラー（plan が取得できない等）では push を止めない（fail-open）。ジョブ自体の失敗では止める（fail-closed）。fail-open で抜けた場合も、push 後の非同期実行が commit status を返すため検出は担保される。
- 緊急時のみ `LOCAL_CI_GATE=0 git push` で無効化できる。**常用は禁止**（使ったら理由を残す）。

## 重い層: push 後の非同期実行

- `local-ci watch --once` を launchd（`io.github.y-kanekoo.local-ci`、900 秒間隔）で回すのが標準構成。`~/.local-ci/config.yml` に `report: status`、`branches: [main, master]`、`watch_repos`（監視対象のチェックアウト）を置く。
- watch は PR の head SHA と対象ブランチの新コミットを `~/.local-ci/state.json` で記録し、未実行分だけ全ステージを実行して commit status を返す。
- 初回導入時は state.json に現在
...[truncated 1909 chars]