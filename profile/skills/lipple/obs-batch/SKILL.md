---
name: obs-batch
description: 監視PDCAチャンネルの新着をまとめ処理し、予定工数/実測/予実/表記候補を state に書く決定論バッチ。判断・文面は findings に積むだけで自動投稿しない。
metadata:
  hermes:
    tags: [observation, deterministic, cron]
---

# obs-batch（§3.1/3.2/3.3/3.4/3.5-Layer1）

監視PDCA（#5035-yu-pdca）の観測本体。**`scripts/run.py` を cron で `--no-agent`/`--script` 実行＝LLM を呼ばない・API 課金ゼロ**。

## やること（すべて決定論。`lib/observe.py`）
- 朝スケジュール「…の予定です」→ `state/plan_<date>.json`（予定工数）。
- 開始↔終了の ts 差 → `state/actuals_<date>.json`（実測・予実差・突合）。
- `state/channel_timers.json` 更新（`last_post_ts`／`end_of_work_date`／`last_processed_ts`）。silence-reminder がこれを読む。
- 表記 Layer1（用語辞書照合：`sns→SNS`・誤変換・固有名詞ゆれ）→ ヒットは `state/findings.jsonl` に `kind:notation` で積む。
- 突合失敗（§3.7）→ `findings.jsonl` に `kind:reconcile_fail`（確認は1回・propose 側で）。

## 呼ばないもの
- LLM。表記の**採否・文面**、突合の**曖昧解決**は判断＝ findings に積むだけ。実際に表へ出すのは propose-to-approval（承認系）／本番期は直接。

## cron
`*/10 9-19 * * 1-5`（latency ≤10分で十分）。

## 実行
`HERMES_FIXTURES`（ローカル）か `SLACK_BOT_TOKEN`（box）で `lib/source.py` がメッセージ源を切替。用語辞書は `HERMES_NOTATION_RULES`（無ければ fixtures）。
