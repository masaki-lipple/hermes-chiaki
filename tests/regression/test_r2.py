#!/usr/bin/env python3
"""再設計R2（イベント駆動化）のテスト。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_r2"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
REPO = "/Users/malus_bot/Claude/Hermes"
if not (SCRATCH / "skills").exists():
    os.symlink(f"{REPO}/profile/skills", SCRATCH / "skills")
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = REPO
sys.path.insert(0, REPO)

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
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
R = f"{REPO}/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)
MGMT = runtime.CH_CHIAKI_MGMT
MENTION = g["MENTION"]

runtime.save_json("pending_approvals.json", {"items": {"500.0": {"status": "pending"}}})
runtime.save_json("chiaki_intake.json", {"items": {}})
runtime.save_json("codex_threads.json", {"items": {}})

# Slackの現物（読み直し先）
threads = {
    "10.0": [{"ts": "10.0", "ts_float": now - 60, "user_id": runtime.TODA,
              "text": f"{MENTION} これ直せる？（編集済みの最新文）"}],
    "500.0": [{"ts": "500.0", "ts_float": now - 9999, "user_id": "U0BCCMPKD54", "text": "提案"},
              {"ts": "501.0", "ts_float": now - 50, "user_id": runtime.TODA, "text": "GO"}],
    "30.0": [{"ts": "30.0", "ts_float": now - 120, "user_id": "U09T44VEZM1",
              "text": f"{MENTION} 教えて"}],
    "40.0": [{"ts": "40.0", "ts_float": now - 9999, "user_id": "U09T44VEZM1", "text": "根"},
             {"ts": "41.0", "ts_float": now - 60, "user_id": "U09T44VEZM1",
              "text": f"{MENTION} スレッド内から"}],
}
source.read_thread = lambda ch, root: threads.get(root, [])

# 台帳行を仕込む
ledger.record("C1:10.0", source="listener", actor=runtime.TODA, ch="C1", thread_root="10.0",
              ts="10.0", text="古いスナップショット", owner="intake", status="received")
ledger.record(f"{MGMT}:501.0", source="listener", actor=runtime.TODA, ch=MGMT,
              thread_root="500.0", ts="501.0", text="GO", owner="intake", status="received")   # 裁定領分
ledger.record("C1:20.0", source="listener", actor=runtime.TODA, ch="C1", thread_root="20.0",
              ts="20.0", text="消えた発話", owner="intake", status="received")                 # 削除済み
ledger.record("C1:30.0", source="listener", actor="U09T44VEZM1", ch="C1", thread_root="30.0",
              ts="30.0", text="x", owner="intake", status="received")                          # escalate対象
ledger.record("C1:41.0", source="listener", actor="U09T44VEZM1", ch="C1", thread_root="40.0",
              ts="41.0", text="x", owner="intake", status="received")                          # スレッド内=対象外
ledger.record("C9:90.0", source="listener", actor=runtime.TODA, ch="C9", thread_root="90.0",
              ts="90.0", text="x", owner="apply", status="received")                           # apply行=無視
ledger.record("C1:70.0", source="listener", actor=runtime.TODA, ch="C1", thread_root="70.0",
              ts="70.0", text="x", owner="intake", status="handled")                           # 処理済み=無視

cand = g["_ledger_candidates"]({})
got = {(c[0]["ts"], c[3]) for c in cand}
check("toda row -> candidate with fresh text",
      any(c[0]["ts"] == "10.0" and "編集済みの最新文" in c[0]["text"] for c in cand))
check("bare GO in ruling thread skipped", ("501.0", "") not in got)
check("gone message skipped", not any(t == "20.0" for t, _ in got))
check("escalate top-level fresh", ("30.0", "escalate") in got)
check("non-toda thread reply skipped", not any(t == "41.0" for t, _ in got))
check("apply row ignored", not any(t == "90.0" for t, _ in got))
check("handled row ignored", not any(t == "70.0" for t, _ in got))
e = ledger.entry(f"{MGMT}:501.0")
check("ruling skip recorded", e["status"] == "skipped" and e["note"] == "apply領分")
check("gone skip recorded", ledger.entry("C1:20.0")["status"] == "skipped")

# リコンサイルの間隔制御: __scan__ が新しければ _candidates は呼ばれない
calls = {"n": 0}
def fake_scan(cur, items):
    calls["n"] += 1
    return []
g["_candidates"] = fake_scan
g["_ledger_candidates"] = lambda items: []
runtime.save_json("tuning_cursor.json", {"__scan__": now})
g["main"]()
check("reconcile throttled", calls["n"] == 0)
runtime.save_json("tuning_cursor.json", {"__scan__": now - 9999})
g["main"]()
check("reconcile fires after interval", calls["n"] == 1)
check("scan cursor updated", float(runtime.load_json("tuning_cursor.json", {})["__scan__"]) > now - 60)

# listener: filed(24h)スレッドの即時対象化
L = f"{REPO}/profile/skills/lipple/event-listener/scripts/run.py"
gl = {"__file__": L, "__name__": "listener_mod"}
exec(compile(open(L).read(), L, "exec"), gl)
runtime.save_json("chiaki_intake.json", {"items": {
    "a": {"status": "filed", "channel": "CX", "thread_root": "100.0", "proposed_at": now - 3600},
    "b": {"status": "filed", "channel": "CX", "thread_root": "200.0", "proposed_at": now - 200000}}})
check("filed fresh -> intake thread", gl["_is_intake_thread"]("CX", "100.0"))
check("filed stale -> not intake thread", not gl["_is_intake_thread"]("CX", "200.0"))

print(f"\n{ok} checks passed")
