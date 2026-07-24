#!/usr/bin/env python3
"""R4-1 日次サマリ（2026-07-24 戸田「R4-1いこうか」＝Issue「1. R4-1 日次サマリ」）のテスト。
決定論集計・日付ガード・空の日・祝日スキップ・投稿失敗時の再試行余地。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_ds"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
REPO = "/Users/malus_bot/Claude/Hermes"
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = REPO
sys.path.insert(0, REPO)

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: ""
sys.modules["lib.llm"] = fake_llm

from lib import ledger, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

now = runtime.now_ts()
R = f"{REPO}/profile/skills/lipple/daily-summary/scripts/run.py"
g = {"__file__": R, "__name__": "ds_mod"}
exec(compile(open(R).read(), R, "exec"), g)
runtime.is_jp_workday = lambda ts=None: True

# ── きょうのデータを仕込む（日付境界に依存しない＝深夜実行でも壊れない） ──
d0 = g["_day_start"](now)
def today(off):
    return max(d0 + 1, now - off)  # 「きょうの中で now 以前」に丸める
yesterday = d0 - 10
rows = [
    {"id": "C1:1.0", "at": today(3600), "owner": "intake", "status": "received"},
    {"id": "C1:1.0", "at": today(3500), "status": "handled"},
    {"id": "C1:2.0", "at": today(3000), "owner": "intake", "status": "skipped"},
    {"id": "C1:3.0", "at": today(2000), "owner": "codex", "status": "queued"},
    {"id": "C1:9.0", "at": yesterday, "owner": "intake", "status": "handled"},  # きのう=対象外
]
for r in rows:
    runtime.append_jsonl(ledger.FILE, r)
runtime.append_jsonl("rulings.jsonl", {"ts": today(1800), "verdict": "go"})
runtime.append_jsonl("rulings.jsonl", {"ts": today(1700), "verdict": "expired"})
runtime.append_jsonl("rulings.jsonl", {"ts": yesterday, "verdict": "reject"})  # きのう=対象外
runtime.append_jsonl("findings.jsonl", {"ts": today(1600), "kind": "typo"})
runtime.append_jsonl("findings.jsonl", {"ts": today(1500), "kind": "typo"})
runtime.save_json("pending_approvals.json", {"items": {
    str(today(1000)): {"status": "pending"},
    "100.0": {"status": "awaiting_completion"}}})
runtime.append_jsonl("llm_usage.jsonl", {"ts": today(900), "model": "GPT 5.5"})
runtime.append_jsonl("llm_usage.jsonl", {"ts": today(800), "model": "Haiku 4.5"})

sent = []
source.post_message = lambda ch, text: sent.append((ch, text)) or {"ok": True, "ts": "77.0"}
g["main"]()
check("posted once", len(sent) == 1 and sent[0][0] == runtime.CH_CHIAKI_MGMT)
t = sent[0][1]
check("self mention (no daily ping)", t.startswith(f"<@{runtime.CHIAKI_SELF}>"))
check("intake counts today only", "• 受付: 2件" in t and "処理済み1" in t and "スキップ1" in t)
check("rulings today only", "GO1" in t and "自動失効1" in t and "却下" not in t)
check("codex line", "• Codex対話: 1件" in t and "キュー投入1" in t)
check("findings line", "誤字2" in t)
check("backlog line", "裁定待ち1件・修正の完了待ち1件" in t)
check("new proposal line", "新しい提案: 1件" in t)
check("llm line", "LLM呼び出し: 2回" in t and "GPT 5.5=1" in t)

# ── 日付ガード＝同日2回目は投稿しない ──
g["main"]()
check("date guard", len(sent) == 1)

# ── 投稿失敗＝状態を進めない（同日の再実行で再試行できる） ──
runtime.save_json("daily_summary.json", {})
source.post_message = lambda ch, text: {"ok": False}
g["main"]()
check("post failure keeps state", not runtime.load_json("daily_summary.json", {}).get("date"))

# ── 動きが無い日＝短い1行 ──
for f in ("exec_ledger.jsonl", "rulings.jsonl", "findings.jsonl", "llm_usage.jsonl"):
    (SCRATCH / "state" / f).unlink()
runtime.save_json("pending_approvals.json", {"items": {}})
sent.clear()
source.post_message = lambda ch, text: sent.append((ch, text)) or {"ok": True, "ts": "78.0"}
g["main"]()
check("quiet day short message", len(sent) == 1 and "動きはありませんでした" in sent[0][1])

# ── 祝日はスキップ ──
runtime.save_json("daily_summary.json", {})
runtime.is_jp_workday = lambda ts=None: False
sent.clear()
g["main"]()
check("holiday skip", not sent)

print(f"\n{ok} checks passed")
