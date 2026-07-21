#!/usr/bin/env python3
"""URL行の前に空行（2026-07-21 戸田「URLの上は改行したい」）のテスト。"""
import os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path(__file__).parent / "state_us"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
sys.path.insert(0, str(ROOT))
from lib import source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)
    ok += 1

f = source._blank_before_urls
check("blank inserted before url",
      f("登録しました！\nhttps://app.notion.com/p/x") == "登録しました！\n\nhttps://app.notion.com/p/x")
check("consecutive urls stay together",
      f("根拠:\nhttps://a.example\nhttps://b.example")
      == "根拠:\n\nhttps://a.example\nhttps://b.example")
check("already blank not doubled",
      f("本文。\n\nhttps://a.example") == "本文。\n\nhttps://a.example")
check("inline url untouched", f("詳細は https://a.example を見てください。")
      == "詳細は https://a.example を見てください。")
check("bracketed slack url", f("本文。\n<https://a.example|リンク>") == "本文。\n\n<https://a.example|リンク>")
check("channel links untouched", f("一覧:\n<#C012345>\n<#C023456>") == "一覧:\n<#C012345>\n<#C023456>")
check("code fence untouched", f("```\n本文\nhttps://a.example\n```")
      == "```\n本文\nhttps://a.example\n```")
check("url at head untouched", f("https://a.example\n本文。") == "https://a.example\n本文。")

print(f"\n{ok} checks passed")
