#!/usr/bin/env python3
"""intake窓口の動的化＋リマインドA事実のテスト（2026-07-10「なぜ無視される」）。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_w"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = "/Users/malus_bot/Claude/Hermes"
sys.path.insert(0, "/Users/malus_bot/Claude/Hermes")

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: ""
sys.modules["lib.llm"] = fake_llm

from lib import convo, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

R = "/Users/malus_bot/Claude/Hermes/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)

MGMT, PDCA = runtime.CH_CHIAKI_MGMT, runtime.CH_CHIAKI_PDCA
MENTION = g["MENTION"]

# 1. _watch_channels: 動的一覧から#8902/#5902を除外
source.list_bot_channels = lambda: [{"id": MGMT, "name": "8902"}, {"id": PDCA, "name": "5902"},
                                    {"id": "C0BAF91KM5K", "name": "a040"}, {"id": "CAAA", "name": "a001"}]
check("watch dynamic", g["_watch_channels"]() == ["C0BAF91KM5K", "CAAA"])

# 2. _watch_channels: API不通はフォールバック
def _boom():
    raise RuntimeError("api down")
source.list_bot_channels = _boom
check("watch fallback", g["_watch_channels"]() == [runtime.CH_YU_PDCA, runtime.CH_NICHIJI])

# 3. _candidates: 初見チャンネルは6時間だけ遡る
now = runtime.now_ts()
recent_mention = {"ts": "111.1", "ts_float": now - 2 * 3600, "user_id": runtime.TODA,
                  "text": f"{MENTION} これどういうロジック？", "datetime": "2026-07-10 20:27"}
old_mention = {"ts": "222.2", "ts_float": now - 8 * 3600, "user_id": runtime.TODA,
               "text": f"{MENTION} 古い呼びかけ", "datetime": "2026-07-10 13:00"}
source.list_bot_channels = lambda: [{"id": "CNEW", "name": "a040"}]
source.read_recent = lambda ch, oldest_ts=None, limit=200, **k: (
    [recent_mention, old_mention] if ch == "CNEW" else [])
source.read_thread = lambda ch, root: []
cand = g["_candidates"]({}, {})
got = [(m["ts"], ch) for m, root, ch, hint in cand]
check("new channel picks 2h-old mention", ("111.1", "CNEW") in got)
check("new channel skips 8h-old mention", ("222.2", "CNEW") not in got)

# 4. カーソルが既にあるチャンネルは従来どおりカーソル基準
cand = g["_candidates"]({"CNEW": now - 9 * 3600}, {})
got = [m["ts"] for m, root, ch, hint in cand]
check("existing cursor keeps both", "111.1" in got and "222.2" in got)

# 5. thread_facts: リマインドAの事実
runtime.save_json("task_follow.json", {"A:CNEW:100.0:105.5": {"ts": now - 12 * 3600}})
convo.source.read_thread = lambda ch, root: []
facts = convo.thread_facts("CNEW", "100.0")
check("remind-A fact present", any("リマインドA" in f and "翌営業日" in f for f in facts))
check("remind-A fact absent for other thread", not any("リマインドA" in f for f in convo.thread_facts("CNEW", "999.0")))

print(f"\n{ok} checks passed")
