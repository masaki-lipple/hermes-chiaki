---
name: chiaki-tuning
description: 戸田さんが #8902 に書いた Chiaki AI への口頭フィードバック/指示を拾い、Haikuで分類して tuning.json に蓄積。silence/pdca/propose の文面生成が必ず反映する。会話エージェントは使わない。
metadata:
  hermes:
    tags: [feedback, learning, llm, approval]
    model: claude-haiku-4-5
---

# chiaki-tuning（口頭フィードバックの学習・反映）

「誤字脱字の #8902 裁定ループ」と同じ発想で、**Chiaki AI 自身の振る舞い・文面への調整**も Slack で受けて効かせる。

## 使い方（戸田さん）
- **#8902 にトップレベルで書くだけ**。例:「silenceの文面、もっと言い切りで」「PDCAの所感はもっと具体的に」「提案はクライアント名を出さないで」。
- 提案スレッドへの返信（GO/却下/文面修正＝裁定）は従来どおり apply-ruling が処理。**トップレベル投稿＝chiakiへのフィードバック**として本スキルが拾う。

## 仕組み（決定論＋Haiku・会話エージェント不使用）
1. `run.py` が #8902 の戸田さん新規トップレベル投稿を拾う（`tuning_cursor.json`）。
2. Haiku で `{is_feedback, skill(silence/pdca/propose/notation/stall/general), directive, ack}` に分類。
3. フィードバックなら `tuning.json` の該当 skill に directive を蓄積（skillごと最大8件）。
4. その投稿スレッドに自己メンションで `ack`（「承知しました…」）を返す。
5. **silence/pdca/propose の生成が `runtime.load_tuning(skill)` を読み、Haiku プロンプトに「戸田さんの指示（必ず守る）」として注入**＝以後の文面に反映。

## cron
`*/2 9-19 * * 1-5`（≤2分で反映）。LLM はフィードバックがある時だけ起動。
