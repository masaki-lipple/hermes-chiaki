#!/usr/bin/env python3
"""ledger-notion（実行台帳のNotion日次同期・2026-07-21）のテスト。"""
import os, sys, types
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path(__file__).parent / "state_ln"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = str(ROOT)
sys.path.insert(0, str(ROOT))

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: ""
sys.modules["lib.llm"] = fake_llm

from lib import ledger, notion, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)
    ok += 1

R = str(ROOT / "profile/skills/lipple/ledger-notion/scripts/run.py")
g = {"__file__": R, "__name__": "ln_mod"}
exec(compile(open(R).read(), R, "exec"), g)
now = runtime.now_ts()

# summarize: 遷移・結果・応答秒数・リンク・承認
es = [
    {"id": "C1:10.0", "at": now - 100, "source": "listener", "actor": "U9R35H06L", "ch": "C1",
     "thread_root": "9.0", "ts": "10.0", "text": "<@U0BCCMPKD54>\nなおして！", "owner": "intake",
     "status": "received"},
    {"id": "C1:10.0", "at": now - 90, "status": "failed", "note": "HTTPError: 529"},
    {"id": "C1:10.0", "at": now - 60, "status": "handled"},
    {"id": "C1:10.0", "at": now - 30, "status": "skipped", "note": "already_replied"},
]
s = g["summarize"](es)
check("outcome handled", s["outcome"] == "処理済み")
check("latency 40s", s["latency"] == 40)
check("trans string", s["trans"].startswith("受信(+0s)→失敗(+10s)→処理済み(+40s)"))
check("mention stripped", s["title"] == "なおして！")
check("already_replied excluded from note", "already_replied" not in s["note"] and "529" in s["note"])
check("link built", "archives/C1/p100" in s["link"] and "thread_ts=9.0" in s["link"])

# ruled+approval
es2 = [{"id": "C2:20.0", "at": now - 50, "source": "listener", "actor": "U9R35H06L", "ch": "C2",
        "ts": "20.0", "text": "GO", "owner": "apply", "status": "received"},
       {"id": "C2:20.0", "at": now - 45, "status": "ruled",
        "refs": {"approval": {"digest": "abc123", "verdict": "go"}}}]
s2 = g["summarize"](es2)
check("ruled outcome", s2["outcome"] == "裁定済み" and "abc123" in s2["note"])

# main: 既存IDはスキップ・新規のみ作成・変化した直近行は更新
runtime.save_json = runtime.save_json  # noop
for e in es + es2:
    runtime.append_jsonl(ledger.FILE, e)
os.environ["NOTION_INTEGRATION_TOKEN"] = "tok"
existing = {"C1:10.0": {"page_id": "pg1", "trans": "古い遷移", "outcome": "処理済み"}}
g["_existing_rows"] = lambda: dict(existing)
source.list_bot_channels = lambda: [{"id": "C1", "name": "5902"}, {"id": "C2", "name": "8902"}]
source.user_display_name = lambda uid: "戸田"
made, upd = [], []
notion._create_page = lambda db, props, label: made.append(props) or "http://x"
notion.update_page_props = lambda pid, props: upd.append((pid, props)) or True
g["main"]()
check("new row created", len(made) == 1 and made[0]["ID"]["rich_text"][0]["text"]["content"] == "C2:20.0")
check("changed row updated", len(upd) == 1 and upd[0][0] == "pg1")
check("select props shape", made[0]["状態"]["select"]["name"] == "裁定済み")

# 未共有（query失敗）は静かにスキップ
g["_existing_rows"] = lambda: None
made.clear(); upd.clear()
g["main"]()
check("unshared -> skip", not made and not upd)

print(f"\n{ok} checks passed")
