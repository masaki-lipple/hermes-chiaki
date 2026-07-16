# 回帰テスト（セッション横断の資産）

Claude Code セッションで書いた機能・修正のテスト。実行: `python3 tests/regression/test_*.py`（各自独立・
状態は test 内の state_*/ に隔離・Slack/Notion/LLM はモック＝外部アクセスなし）。
元は scratchpad にあったが /tmp の自動掃除で消えるためリポジトリへ移設（2026-07-14）。
