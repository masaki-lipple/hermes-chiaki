#!/usr/bin/env python3
"""編集失敗→Issue提案への切り替え＋観測チャンネル降順（2026-07-16）のテスト。"""
import os, sys, types
from pathlib import Path
SCRATCH = Path(__file__).parent / "state_ep"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = "/Users/malus_bot/Claude/Hermes"
sys.path.insert(0, "/Users/malus_bot/Claude/Hermes")

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: "GPT 5.5"
sys.modules["lib.llm"] = fake_llm

from lib import convo, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)
    ok += 1

# ── convo: edit_postの判断基準（定型投稿の仕組み変更はpropose） ──
check("edit_post guidance in actions", "propose（issue=コード変更）" in convo.ACTIONS["edit_post"])

# ── intake: 編集失敗(norevise)→Issue提案へ切り替え ──
R = "/Users/malus_bot/Claude/Hermes/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)
posted = []
g["_reply"] = lambda ch, root, text, *a, **k: posted.append(text)
g["_bug_symptom"] = lambda *a, **k: None
g["_maybe_edit_root"] = lambda *a, **k: "norevise"
g["_progress_watch"] = lambda *a, **k: (lambda: None)
convo.decide = lambda *a, **k: {"action": "edit_post", "reply": "了解です！並び替えます。",
                                "instruction": "観測開始メッセージのチャンネル一覧を英数字降順にする"}
items = {}
m = {"ts": "10.0", "ts_float": 1.0, "user_id": runtime.TODA,
     "text": "チャンネルの順番を英数字降順にしてほしい！"}
r = g["_propose_agent"](m, runtime.CH_CHIAKI_PDCA, "9.0", items)
check("edit-fail pivots to proposal", r is not None and any("Issueに起票して" in t for t in posted))
it = next(iter(items.values()), {})
check("awaiting_confirm with issue bill", it.get("status") == "awaiting_confirm"
      and it["proposals"][0]["type"] == "issue" and "降順" in it["proposals"][0]["要約"])
check("no dead-end canned text", not any("うまく汲み取れませんでした" in t for t in posted))

# notfound は従来どおり聞き返す
posted.clear()
items.clear()
g["_maybe_edit_root"] = lambda *a, **k: "notfound"
m2 = dict(m, ts="11.0")
g["_propose_agent"](m2, runtime.CH_CHIAKI_PDCA, "9.0", items)
check("notfound keeps asking", any("特定できませんでした" in t for t in posted))

# ── 確認ターン側の同じ切り替え ──
posted.clear()
g["_maybe_edit_root"] = lambda *a, **k: "norevise"
it2 = {"proposals": [{"type": "issue", "要約": "x"}], "status": "awaiting_confirm", "propose_count": 1}
r = g["_confirm_agent"](it2, dict(m, ts="12.0"), runtime.CH_CHIAKI_PDCA, "9.0")
check("confirm edit-fail pivots", r == 1 and it2["status"] == "awaiting_confirm"
      and it2["proposals"][0]["issue_kind"] == "変更" and any("Issueに起票して" in t for t in posted))

# ── chiaki-pdca: 観測チャンネル英数字降順 ──
C = "/Users/malus_bot/Claude/Hermes/profile/skills/lipple/chiaki-pdca/scripts/run.py"
gc = {"__file__": C, "__name__": "pdca_mod"}
exec(compile(open(C).read(), C, "exec"), gc)
source.list_bot_channels = lambda: [{"id": "C1", "name": "a010-zebra"}, {"id": "C2", "name": "c030-alpha"},
                                    {"id": "C3", "name": "b020-mid"}]
check("channels descending by NAME", gc["_observed_channels"]() == ["C2", "C3", "C1"])

# ── 全LLM不通: 初回失敗だけ決定論の固定文・2回目以降は沈黙 ──
from lib import ledger
posted.clear()
def boom(*a, **k):
    raise RuntimeError("529 all llm down")
g["_find_awaiting"] = lambda *a, **k: None
g["_propose_agent"] = boom
g["_handle_propose"] = boom
g["_candidates"] = lambda cur, items: []
g["_ledger_candidates"] = lambda items: [
    ({"ts": "50.0", "ts_float": 50.0, "user_id": runtime.TODA, "text": "これ直して"}, "49.0", "CX", "")]
runtime.save_json("chiaki_intake.json", {"items": {}})
runtime.save_json("tuning_cursor.json", {"__scan__": runtime.now_ts()})
g["main"]()
check("first failure -> honest note", any("不調" in t for t in posted))
posted.clear()
g["main"]()
check("second failure -> silent", not posted)

print(f"\n{ok} checks passed")
