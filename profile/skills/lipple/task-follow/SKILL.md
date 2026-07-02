---
name: task-follow
description: task_ledger.json を元に、完了報告の確認待ちを決定論でリマインドする。
metadata:
  hermes:
    tags: [observation, deterministic, cron]
---

# task-follow

業務タスクの追跡リマインド。`stall-scan` が生成した `state/task_ledger.json` を読み、Slack スレッドへ機械的に1回だけ促す。

## cron

`50 8 * * 1-5`

## 仕様

- 完了スタンプ `kanryo` があるタスクは対象外。
- 完了報告があり、報告内の責任者メンションが翌日以降も未返信なら責任者へ確認催促。
- 期限当日の未報告リマインドは扱わない（Task AI が8:30に「このタスクは本日が対応期限です。」を送る領分・2026-07-03 戸田決定）。
- LLM は使わない。
