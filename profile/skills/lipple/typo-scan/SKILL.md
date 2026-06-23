---
name: typo-scan
description: 用語辞書に載っていない汎用の誤字・脱字（例「文字お越し」「修正しまた」）をHaikuで拾う。§3.5 Layer2。1回のHaikuバッチで当日新着（投稿元＋スレッド返信）をまとめて校正。
metadata:
  hermes:
    tags: [quality, llm, cron, approval]
    model: claude-haiku-4-5
---

# typo-scan（§3.5 Layer2＝汎用誤字・Haiku・保守的）

辞書（用語辞書DB）で直せるものは obs-batch の Layer1（決定論 notation_check）が拾う。**ここは辞書に無い一般的な誤字・脱字・誤変換**を拾う。意味を読まないと判定できないので Haiku。

## 実装（`scripts/run.py`・決定論フロー＋Haiku 1回）
- 対象: `#5035` の**当日新着**（`typo_cursor.json` 以降）。**投稿元＋スレッド返信**の両方。bot/自分(Chiaki AI)・空文は除外。
- **1回の Haiku 呼び出し**で全新着をまとめて校正（メッセージごとに呼ばない＝低コスト）。
- **保守的プロンプト**: 明確な誤字だけ。固有名詞・製品名・人名・社内既知用語（notation_rules の terms/acronyms を「正しい語」として渡す）・意図的な英字大小・口語/スタイルは指摘しない。確信が無いものは出さない。
- 辞書層(notation_check)と **found が重複するものはスキップ**（二重提案防止）。
- 検知は `findings.jsonl` に `kind:typo` で積むだけ。採否・文面・投稿は **propose-to-approval → 戸田承認 → apply-ruling**（notation と同じループ。完了検証・リマインドも共通）。

## cron
`50 17 * * 1-5`（当日全部・松永さんの定時18:00直前）＋ `30 18 * * 1-5`（17:50以降の差分）。LLM はこの2回だけ＝コスト最小。
※ `scripts/gather.py` は旧・素材出力用（agent 前提）。現行は `run.py` が検知まで完結する。
