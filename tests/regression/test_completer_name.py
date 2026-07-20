#!/usr/bin/env python3
"""完了通知の名義=実際に報告した人（2026-07-20 レビューで確定した誤帰属の根治）のテスト。"""
import os, sys, types
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path(__file__).parent / "state_cn"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = str(ROOT)
sys.path.insert(0, str(ROOT))

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.haiku = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: ""
sys.modules["lib.llm"] = fake_llm

from lib import runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)
    ok += 1

A = str(ROOT / "profile/skills/lipple/apply-ruling/scripts/run.py")
ga = {"__file__": A, "__name__": "apply_mod"}
exec(compile(open(A).read(), A, "exec"), ga)

MATSUNAGA, MATSUSHITA = "U09T44VEZM1", "U0SHOKO0001"
names = {MATSUNAGA: "Yu Matsunaga", MATSUSHITA: "Shoko Matsushita"}
source.user_display_name = lambda uid: names.get(uid, "")
posted = []
source.post_thread_reply = lambda ch, ts, text: posted.append((ch, ts, text)) or {"ok": True, "ts": "77.7"}

def run_case(replies, verify):
    posted.clear()
    ga["_verify_fixed"] = lambda it, r: verify
    it = {"status": "awaiting_completion", "source_channel": "CSRC", "source_ts": "5.0",
          "target_user_id": MATSUNAGA, "target_name": "Yu Matsunaga",
          "nudge_ts": "10.0", "draft": "x", "verify_found": "ですです"}
    source.read_thread = lambda ch, root: [{"ts": "5.0", "user_id": MATSUNAGA, "text": "本文"}] + replies
    pend = {"items": {"100.0": it}}
    r = ga["_complete_one"](pend, "100.0", it) if "_complete_one" in ga else ga["_phase_completion"](pend)
    return it, posted

# 1. 対象者と別の人（松下さん）が完了報告 → お礼も通知も松下さん名義
it, out = run_case([{"ts": "20.0", "user_id": MATSUSHITA, "text": "修正しました！"}], None)
check("completed by other", it["status"] == "completed")
check("thanks to actual completer", any(f"<@{MATSUSHITA}>" in t for _, _, t in out))
check("notice names Matsushita", any("Shoko Matsushitaさんが修正を完了しました" in t for _, _, t in out))
check("notice not Matsunaga", not any("Yu Matsunagaさんが修正" in t for _, _, t in out))

# 2. 報告なし・自動検証のみ → 人名を断定しない
it, out = run_case([], True)
check("completed by verify", it["status"] == "completed")
check("no name attribution", any("修正されていることを確認しました" in t for _, _, t in out)
      and not any("さんが修正を完了しました" in t for _, _, t in out))

# 3. 対象者本人の報告 → 従来どおり本人名義
it, out = run_case([{"ts": "21.0", "user_id": MATSUNAGA, "text": "直しました！"}], None)
check("self-completion named", any("Yu Matsunagaさんが修正を完了しました" in t for _, _, t in out))

print(f"\n{ok} checks passed")
