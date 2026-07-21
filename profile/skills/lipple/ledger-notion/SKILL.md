---
name: ledger-notion
description: 実行台帳（exec_ledger.jsonl）を実行台帳_Chiaki_AI_DB（Notion）へ日次同期。1依頼=1行・IDで重複防止・状態が変わった行は更新。決定論・LLM非起動。
metadata:
  hermes:
    tags: [observation, deterministic, cron]
---

# ledger-notion（実行台帳のNotion控え・2026-07-21 戸田「その後Notionで実装したい」）

正本はVPSローカルの exec_ledger.jsonl（会話の実行経路をNotion可用性に縛らない）。
本スキルは閲覧用の控えとして、Notionの実行台帳_Chiaki_AI_DBへ日次で写す（aiko §13「日中は同期書き込みしない・
深夜の日次バッチが台帳へ自動登録」と同型）。

- 1依頼（発話）=1行。DBの「ID」列（channel:ts）で重複防止＝再実行・バックフィルと衝突しない。
- 直近14日の行は状態・遷移が変わっていれば更新（それより古い行は不変）。
- ⚠️ DBはHermes Agentインテグレーションへの共有が必要（未共有時はログのみで静かに終了）。

## cron
`40 21 * * 1-5`
