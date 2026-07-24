#!/usr/bin/env python3
"""Issue「10. 運用の磨き」+「3. Phase D 会話の週次自己レビュー」（2026-07-24 戸田「10、3とりあえず」）のテスト。
10-a: 却下学習の可視化（convo事実） 10-b: 実行台帳のコンパクション 10-c: 裁定黙殺の監査
3: convo-review（週次セルフレビュー投稿・ガード・冪等）。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_pr"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
REPO = "/Users/malus_bot/Claude/Hermes"
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = REPO
sys.path.insert(0, REPO)

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.haiku = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: "GPT 5.5"
sys.modules["lib.llm"] = fake_llm

from lib import convo, ledger, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

now = runtime.now_ts()
MGMT = runtime.CH_CHIAKI_MGMT

# ════ 10-a: 却下学習の可視化（convo事実） ════
source.read_thread = lambda ch, root: []
runtime.save_json("pending_approvals.json", {"items": {}})
facts = "\n".join(convo.thread_facts("CX", "1.0"))
check("10a no learn -> no fact", "却下学習済み" not in facts)
runtime.append_jsonl("reject_learn.jsonl", {"ts": now, "kind": "typo", "found": "以", "reason": "誤検知"})
runtime.append_jsonl("reject_learn.jsonl", {"ts": now, "kind": "typo", "found": "白岩様", "reason": "人物名"})
facts = "\n".join(convo.thread_facts("CX", "1.0"))
check("10a learned founds visible in facts", "「以」" in facts and "「白岩様」" in facts
      and "再指摘しない" in facts)

# ════ 10-b: 実行台帳のコンパクション ════
old_at, new_at = now - 30 * 86400, now - 3600
rows = [
    {"id": "A:1", "at": old_at - 10, "owner": "intake", "status": "received", "text": "x"},
    {"id": "A:1", "at": old_at, "status": "handled"},
    {"id": "B:2", "at": old_at, "owner": "apply", "status": "ruled"},
    {"id": "C:3", "at": new_at - 10, "owner": "intake", "status": "received"},
    {"id": "C:3", "at": new_at, "status": "handled"},
]
for r in rows:
    runtime.append_jsonl(ledger.FILE, r)
before = ledger.load()
removed = ledger.compact(keep_sec=14 * 86400)
after = ledger.load()
raw = runtime.read_jsonl(ledger.FILE)
check("10b merged state preserved", before == after)
check("10b old ids folded to 1 line each",
      removed == 1 and len([r for r in raw if r["id"] == "A:1"]) == 1
      and len([r for r in raw if r["id"] == "C:3"]) == 2)
check("10b folded row keeps fields", next(r for r in raw if r["id"] == "A:1")["text"] == "x"
      and next(r for r in raw if r["id"] == "A:1")["status"] == "handled")
removed2 = ledger.compact(keep_sec=14 * 86400)
check("10b idempotent", removed2 == 0 and ledger.load() == after)

# ════ 10-c: 裁定黙殺の監査（self-health） ════
H = f"{REPO}/profile/skills/lipple/self-health/scripts/run.py"
gh = {"__file__": H, "__name__": "health_mod"}
exec(compile(open(H).read(), H, "exec"), gh)
runtime.append_jsonl(ledger.FILE, {"id": f"{MGMT}:10.0", "at": now - 3600, "owner": "apply",
                                   "status": "received", "ch": MGMT, "ts": "10.0",
                                   "text": "<@U0BCCMPKD54>\nGO"})
runtime.append_jsonl(ledger.FILE, {"id": f"{MGMT}:11.0", "at": now - 3600, "owner": "apply",
                                   "status": "received", "ch": MGMT, "ts": "11.0",
                                   "text": "ありがとう！"})
runtime.append_jsonl(ledger.FILE, {"id": f"{MGMT}:12.0", "at": now - 600, "owner": "apply",
                                   "status": "received", "ch": MGMT, "ts": "12.0", "text": "GO"})
warns = gh["_ruling_swallowed"](now)
check("10c unprocessed GO warned", len(warns) == 1 and "ts=10.0" in warns[0])

# ════ 3: convo-review ════
R = f"{REPO}/profile/skills/lipple/convo-review/scripts/run.py"
g = {"__file__": R, "__name__": "review_mod"}
exec(compile(open(R).read(), R, "exec"), g)

def mem_with(n, ts0):
    return {"ledger": [{"ts": ts0 + i, "dt": "07-22 10:00", "ch": MGMT, "root": "1.0",
                        "m_ts": str(ts0 + i), "said": f"依頼{i}", "reply": f"返答{i}",
                        "action": "answer"} for i in range(n)]}

sent = []
source.post_message = lambda ch, text: sent.append((ch, text)) or {"ok": True, "ts": "50.0"}

# 会話が少ない週はスキップ
runtime.save_json(convo.MEM_FILE, mem_with(2, now - 3600))
g["main"]()
check("3 few entries -> skip", not sent)

# LLM不通＝投稿しない・状態も進めない
runtime.save_json(convo.MEM_FILE, mem_with(6, now - 3600))
def gpt_down(*a, **k):
    raise RuntimeError("529")
fake_llm.gpt = gpt_down
g["main"]()
check("3 LLM down -> no post, retryable", not sent
      and not runtime.load_json("convo_review.json", {}).get("last_run_ts"))

# 正常系＝レビュー投稿＋状態保存
fake_llm.gpt = lambda *a, **k: json.dumps({
    "summary": "全体としては自然でした。",
    "issues": [{"no": 2, "problem": "同じ言い回しの繰り返し", "suggestion": "言い換える"}]},
    ensure_ascii=False)
g["main"]()
check("3 review posted", len(sent) == 1 and sent[0][0] == MGMT)
t = sent[0][1]
check("3 post format", "セルフレビュー" in t and "• 1. 同じ言い回しの繰り返し" in t
      and "改善案: 言い換える" in t and "Issueに」のように返信" in t and t.startswith(f"<@{runtime.TODA}>"))
check("3 source conversation cited", "「依頼1」への応答" in t)
check("3 state saved", runtime.load_json("convo_review.json", {}).get("last_run_ts"))

# 3日未満の再実行はスキップ（冪等）
g["main"]()
check("3 rerun within 3d -> skip", len(sent) == 1)

# 問題なしの週＝正直に「問題なし」を短く出す
runtime.save_json("convo_review.json", {})
fake_llm.gpt = lambda *a, **k: json.dumps({"summary": "自然な会話でした。", "issues": []},
                                          ensure_ascii=False)
sent.clear()
g["main"]()
check("3 clean week -> honest no-issues post", len(sent) == 1
      and "大きな問題は見つかりませんでした" in sent[0][1])

print(f"\n{ok} checks passed")
