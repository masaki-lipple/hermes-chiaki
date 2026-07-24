# llm-usage（R5: LLM呼び出しの棚卸し・可視化）

`lib/llm.py` が全LLM呼び出しを `llm_usage.jsonl` に計測（1呼び出し1行: 呼び出し元スキル・
関数・モデル・成否・所要ms・入出力文字数）。このスキルはその集計・可視化。

- `python3 run.py` … 直近7日の集計を標準出力へ（Claude Code・調査用）。
- `python3 run.py post` … 集計を#8902へ投稿（戸田さん向けの「1枚」）。
- `python3 run.py post 30` … 日数指定。

決定論のみ・LLM非起動。cron登録なし（オンデマンド＋将来のR4-1日次サマリが1行版を載せる予定）。
由来: 2026-07-23 ロードマップ「4. R5 コスト計測」（2026-07-24 戸田「R5」でGO）。
コストの目安: GPTはChatGPTサブスク内（追加課金なし）・Haiku/OpusはAPI課金。
