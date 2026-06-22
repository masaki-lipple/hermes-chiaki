---
name: typo-scan
description: 用語辞書に載っていない汎用の誤字・脱字（例「文字お越し」「修正しまた」）をLLMで拾う。§3.5 Layer2。全メッセージをLLMに通さず、当日Haikuバッチ＋相乗りで拾う。
metadata:
  hermes:
    tags: [quality, llm, cron, approval]
    model: claude-haiku-4-5
---

# typo-scan（§3.5 Layer2＝汎用誤字・LLM）

辞書（用語辞書DB）で直せるものは obs-batch の Layer1（決定論）が拾う。**ここは辞書に無い一般的なtypo・脱字**を拾う。意味を読まないと判定できないので LLM（Haiku）。

## コスト規律（決定論ファースト/バッチ寄せを崩さない）
- **全メッセージをLLMに通さない**。2経路でカバー:
  - **相乗り（リアルタイム）**: propose 等が判断でLLMを起動する時、そのメッセージの誤字も一緒に見る（呼び出しを増やさない）。
  - **当日Haikuバッチ**: `scripts/gather.py` が当日分（または17:50以降の差分）を集め、Haiku 1バッチでスキャン。プロンプトキャッシュ＋Batch API で1人1日 数セント。

## 手順
1. `scripts/gather.py [--since <ts>]` で対象メッセージを取得（17:50=当日全部 / 18:30=17:50以降の差分）。
2. 各メッセージを読み、**辞書に無い明らかなtypo・脱字・誤変換**だけ抽出（固有名詞・専門用語は Layer1 の領分なので除外）。クライアント納品物に関わる箇所ほど重く、PDCA本文の軽微typoは軽く。
3. 候補は `findings.jsonl` に `kind:typo` で積む（採否・文面は propose-to-approval＝承認系へ。ここでは投稿しない）。

## cron
`50 17 * * 1-5`（当日分・松永さんの定時18:00直前）＋ `30 18 * * 1-5`（17:50以降の差分のみ）。
