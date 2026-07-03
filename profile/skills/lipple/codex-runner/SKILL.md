---
name: codex-runner
description: 戸田さん承認済みの修正依頼キューをCodex（GPT）に実装させ、ローカルブランチ＋#8902レビュー待ち報告まで行う。
metadata:
  hermes:
    tags: [automation, codex, cron]
---

# codex-runner

Slack起点のコード修正をVPS内で完結させるランナー。デプロイはしない（Claude Codeのレビュー後）。

## 流れ

1. 戸田さんが `@Chiaki AI 〜 Codexで` と依頼 → chiaki-intake がIssue起票のGO時に `codex_queue.jsonl` へ追加。
2. 本スキル（cron `*/10 * * * *`・flock・1起動1件・日次上限5件）がキューを処理:
   - `~/src/hermes-chiaki-codex`（ローカルクローン）で `codex/q<ts>` ブランチを `origin/main` から作成。
   - `codex exec -s workspace-write` にブリーフを渡して実装+自己テスト。
   - 変更があればランナーが commit（Codex自身はcommit禁止＝AGENTS.md）。
   - #8902 へ「レビュー待ち/変更なし/失敗」を報告。
3. Claude Code がレビュー（VPSからdiff取得→独立検証）→採用ならローカルでcommit/push→deploy→完了報告。

## セキュリティ（2026-07-03 戸田合意の枠）

- キュー投入はintakeの確認ターン（戸田さん `U9R35H06L` のGO）のみ。ランナー側でも `requested_by` を再検証（二重ゲート）。
- VPSにGitHub書き込み権限を持たせない（ブランチはVPS内のみ・pushはClaude Code）。
- workspace-writeサンドボックス・タイムアウト30分。
