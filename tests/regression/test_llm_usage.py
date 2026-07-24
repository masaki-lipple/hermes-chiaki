#!/usr/bin/env python3
"""R5コスト計測（2026-07-24 戸田「R5」＝Issue「4. R5 コスト計測」）のテスト。
lib/llm.pyの呼び出し計測（llm_usage.jsonl）と llm-usage スキルの集計・投稿。
このテストだけは本物の lib.llm を読み込む（_call/_gpt_rawを差し替え）。"""
import json
import os
import sys
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_lu"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
REPO = "/Users/malus_bot/Claude/Hermes"
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = REPO
sys.path.insert(0, REPO)

from lib import llm, runtime, source  # noqa: E402  # 本物のllm（フェイク登録はしない）

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

now = runtime.now_ts()
source.post_message = lambda ch, text: {"ok": True, "ts": "1.0"}  # fallback控え・post用

# ── 計測: 成功・失敗・フォールバックが1行ずつ残る ──
llm._call = lambda model, user, system, max_tokens, timeout=30: "はいく出力"
llm._gpt_raw = lambda user, system, timeout: "GPT出力"
out = llm.gpt("こんにちは")
check("gpt returns", out == "GPT出力")
out = llm.haiku("やあ")
check("haiku returns", out == "はいく出力")

def gpt_down(user, system, timeout):
    raise RuntimeError("auth expired")
llm._gpt_raw = gpt_down
out = llm.gpt("フォールバックして")
check("gpt falls back to haiku", out == "はいく出力" and llm.last_used() == "Haiku 4.5・代替")

def call_down(model, user, system, max_tokens, timeout=30):
    raise RuntimeError("529")
llm._call = call_down
try:
    llm.haiku("落ちる")
    raised = False
except RuntimeError:
    raised = True
check("haiku failure raises", raised)

rows = runtime.read_jsonl("llm_usage.jsonl")
kinds = [(r["fn"], r["ok"], r.get("note", "")) for r in rows]
check("usage rows recorded", ("gpt", True, "") in kinds and ("haiku", True, "") in kinds
      and ("gpt", False, "RuntimeError") in kinds and ("haiku", True, "代替") in kinds
      and ("haiku", False, "RuntimeError") in kinds and len(rows) == 5)
check("caller detected", all(r.get("caller") == "test_llm_usage" for r in rows))
check("timing and volume recorded", all("ms" in r and r.get("in", 0) > 0 for r in rows))

# ── 計測がこけても本処理は止めない ──
orig_append = runtime.append_jsonl
def append_fail(name, row):
    raise OSError("disk full")
runtime.append_jsonl = append_fail
llm._gpt_raw = lambda user, system, timeout: "GPT出力2"
check("tracking failure never breaks calls", llm.gpt("計測死んでても動く") == "GPT出力2")
runtime.append_jsonl = orig_append

# ── llm-usage スキル: 集計と投稿 ──
R = f"{REPO}/profile/skills/lipple/llm-usage/scripts/run.py"
g = {"__file__": R, "__name__": "usage_mod"}
exec(compile(open(R).read(), R, "exec"), g)
runtime.append_jsonl("llm_usage.jsonl", {"ts": now - 30 * 86400, "caller": "old", "fn": "gpt",
                                         "model": "GPT 5.5", "ok": True, "ms": 100, "in": 10, "out": 5})
text = g["summarize"](7)
check("summary counts window only", "全5回" in text and "old×" not in text)
check("summary groups caller×model", "test_llm_usage×GPT 5.5" in text
      and "test_llm_usage×Haiku 4.5" in text)
check("summary notes failures", "失敗" in text)
check("summary cost note", "サブスク内" in text and "API課金" in text)

sent = []
source.post_message = lambda ch, text: sent.append((ch, text)) or {"ok": True, "ts": "2.0"}
sys.argv = [R, "post", "7"]
g["main"]()
check("post arg posts to #8902", len(sent) == 1 and sent[0][0] == runtime.CH_CHIAKI_MGMT
      and sent[0][1].startswith(f"<@{runtime.TODA}>"))

# 記録ゼロの期間は正直に「まだ無い」
(SCRATCH / "state" / "llm_usage.jsonl").unlink()
check("empty window honest", "まだありません" in g["summarize"](7))

print(f"\n{ok} checks passed")
