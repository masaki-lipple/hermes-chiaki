#!/usr/bin/env python3
"""実行台帳（再設計R1）のテスト。"""
import os, sys
from pathlib import Path
SCRATCH = Path(__file__).parent / "state_l"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
sys.path.insert(0, "/Users/malus_bot/Claude/Hermes")
from lib import ledger, runtime

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)
    ok += 1

# 1. 記録→読み出し
eid = ledger.event_id("C1", "10.0")
check("event_id", eid == "C1:10.0")
ledger.record(eid, source="listener", ch="C1", ts="10.0", owner="intake", status="received")
e = ledger.entry(eid)
check("received recorded", e["status"] == "received" and e["source"] == "listener")

# 2. 部分更新のマージ＝新しい行が優先・古いフィールドは残る
ledger.record(eid, status="handled", refs={"reply_ts": "11.0"})
e = ledger.entry(eid)
check("merge keeps source", e["source"] == "listener")
check("merge updates status", e["status"] == "handled" and e["refs"]["reply_ts"] == "11.0")

# 3. None値は書かない
ledger.record(eid, note=None)
check("none skipped", "note" not in ledger.entry(eid))

# 4. 複数id
ledger.record("C2:20.0", status="received", owner="codex")
d = ledger.load()
check("two ids", set(d) == {"C1:10.0", "C2:20.0"})

# 5. 破損行があっても生きる（read_jsonl寛容の継承）
with open(SCRATCH / "state" / ledger.FILE, "a") as f:
    f.write('{"broken\n')
ledger.record("C3:30.0", status="received")
check("survives broken line", "C3:30.0" in ledger.load())

print(f"\n{ok} checks passed")
