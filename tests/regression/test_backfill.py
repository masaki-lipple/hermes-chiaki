#!/usr/bin/env python3
"""backfill（Issue #7・2026-07-24 戸田「7やる」）のテスト。
過去日の再構成・既存日の温存・当日除外・obs-batchと同一抽出関数の使用。"""
import json
import os
import sys
import types
import datetime as dt
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_bf"
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

from lib import observe, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

JST = dt.timezone(dt.timedelta(hours=9))
now = runtime.now_ts()
today = dt.datetime.fromtimestamp(now, JST).strftime("%Y-%m-%d")

def day_str(back):
    return dt.datetime.fromtimestamp(now - back * 86400, JST).strftime("%Y-%m-%d")

d1, d2 = day_str(3), day_str(2)  # 過去2日分
def msg(date, i, text):
    tsf = dt.datetime.strptime(date + " 10:00:00", "%Y-%m-%d %H:%M:%S"
                               ).replace(tzinfo=JST).timestamp() + i
    return {"ts": f"{tsf:.6f}", "ts_float": tsf, "datetime": f"{date} 10:00:0{i}",
            "text": text, "user_id": "U09T44VEZM1"}

calls = []
source.read_recent = lambda ch, oldest_ts=None, limit=200, paginate=False, max_pages=10: calls.append(
    (ch, paginate)) or [
    msg(d1, 0, "10:00 タスクA 開始します"), msg(d1, 1, "11:00 タスクA 終了しました"),
    msg(d2, 0, "09:00 タスクB 開始します"), msg(d2, 1, "09:30 タスクB 終了しました"),
    msg(today, 0, "10:00 タスクC 開始します"),
]
observe.extract_task_events = lambda ms: {
    "actuals": [{"task": t, "kind": "求人", "hours": 1.0} for t in
                sorted({m["text"].split()[1] for m in ms})],
    "unmatched": []}
observe.refine_actual_kinds = lambda actuals, ms: None

# d2は既存（ライブ観測分）＝上書きしない
runtime.save_json(f"actuals_{d2}", None) if False else None
(SCRATCH / "state" / f"actuals_{d2}.json").write_text(
    json.dumps({"date": d2, "actuals": [{"task": "既存", "kind": "求人", "hours": 2.0}]},
               ensure_ascii=False))

B = f"{REPO}/profile/skills/lipple/compute-baselines/scripts/backfill.py"
g = {"__file__": B, "__name__": "bf_mod"}
exec(compile(open(B).read(), B, "exec"), g)
sys.argv = [B, "60"]
g["main"]()

f1 = json.loads((SCRATCH / "state" / f"actuals_{d1}.json").read_text())
check("past day reconstructed", f1["date"] == d1 and f1.get("backfilled") is True
      and f1["actuals"][0]["task"] == "タスクA")
f2 = json.loads((SCRATCH / "state" / f"actuals_{d2}.json").read_text())
check("existing live day preserved", f2["actuals"][0]["task"] == "既存"
      and "backfilled" not in f2)
check("today not written (obs-batch domain)",
      not (SCRATCH / "state" / f"actuals_{today}.json").exists())
check("paginated history fetch", calls and calls[0] == (runtime.CH_YU_PDCA, True))

# 再実行は全スキップ（冪等）
g["main"]()
f1b = json.loads((SCRATCH / "state" / f"actuals_{d1}.json").read_text())
check("rerun idempotent", f1b == f1)

print(f"\n{ok} checks passed")
