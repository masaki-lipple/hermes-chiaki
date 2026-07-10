---
name: convo-memory
description: 会話台帳（convo_memory.json の ledger）から「決定・好み・注意点」を短文の長期記憶（notes）に蒸留する夜間バッチ。会話コアPhase C。
metadata:
  hermes:
    tags: [conversation, memory, cron]
---

# convo-memory（会話コアv2 Phase C・スレッドを跨ぐ記憶）

会話コア（lib/convo）が残す会話台帳＝実際に採用された判断の記録を毎晩読み、長く覚えておくべき
「決定・好み・注意点」だけを短文リスト（最大25件）に蒸留して `convo_memory.json` の `notes` を更新する。
notes は decide() が毎回プロンプトに注入＝スレッドを跨いでも「昨日の話」が通じる。

- 判断（何を残すか）は GPT 5.5、検証・保存は決定論（各120字・25件上限・失敗時は旧 notes 温存）。
- 新しい台帳エントリが無い日は LLM を起動しない（コスト・無駄打ち防止）。
- 単発の作業内容は残さない。恒久的な決定・戸田さんの好み・繰り返す注意点だけ。

## cron
`10 21 * * 1-5`（compute-baselines の後・終業後）。
