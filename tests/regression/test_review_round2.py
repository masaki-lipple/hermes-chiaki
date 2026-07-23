#!/usr/bin/env python3
"""2026-07-23 監査レビュー（6件修正のdiffへの多角レビュー・確定17件）の修正テスト。
A:裁定選定の再修正（skipを飛ばし最初の実質発話・編集指示優先・ACK=skip） B:codex propose順序
C:chiaki_intake.jsonのlost update防止（マージ保存＋ロック） D:listenerのreceived上書きガード
E:applyの台帳定期掃除 F:intakeの再試行上限 G:クレーム上限＋ledger-notion表示 H:終端済み再処理ガード。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_rr2"
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

# ════ A: 裁定選定＋ACK ════
A = f"{REPO}/profile/skills/lipple/apply-ruling/scripts/run.py"
ga = {"__file__": A, "__name__": "apply_mod"}
exec(compile(open(A).read(), A, "exec"), ga)
posted = []
source.post_thread_reply = lambda ch, ts, text: posted.append((ch, ts, text)) or {"ok": True, "ts": "99.9"}

check("A ack classified as skip", ga["_classify"]("ありがとうございました！")[0] == "skip")
check("A edit instruction still interpret", ga["_classify"]("一文足して")[0] == "interpret")

def fresh_item(src_ts="5.0"):
    return {"status": "pending", "source_channel": "CSRC", "source_ts": src_ts,
            "target_user_id": "U09T44VEZM1", "draft": "修正をお願いします！",
            "verify_found": "ですです"}

def mk_read(mgmt_replies, root_key):
    def _read(ch, root):
        if ch == MGMT and root == root_key:
            return [{"ts": root_key, "user_id": runtime.CHIAKI_SELF, "text": "提案"}] + mgmt_replies
        return [{"ts": root, "user_id": "U09T44VEZM1", "text": "本文ですです"}]
    return _read

# GO→ありがとう: GOが読まれる（⑥bの意図は維持）
source.read_thread = mk_read([{"ts": "801.0", "user_id": runtime.TODA, "text": "GO"},
                              {"ts": "802.0", "user_id": runtime.TODA, "text": "ありがとう！"}], "800.0")
it = fresh_item()
r = ga["_rule_one"]({"items": {"800.0": it}}, "800.0", it)
check("A GO behind thanks adopted", r == 1 and it["status"] == "awaiting_completion"
      and ledger.entry(f"{MGMT}:801.0").get("status") == "ruled")

# GO→編集指示: 新しい編集指示が勝つ（旧⑥bの退行の根治）
fake_llm.haiku = lambda *a, **k: "修正をお願いします！確認したら教えてください。"
source.read_thread = mk_read([{"ts": "811.0", "user_id": runtime.TODA, "text": "GO"},
                              {"ts": "812.0", "user_id": runtime.TODA, "text": "最後に確認の一文を足して"}],
                             "810.0")
posted.clear()
it2 = fresh_item("6.0")
r = ga["_rule_one"]({"items": {"810.0": it2}}, "810.0", it2)
check("A newer edit instruction wins over old GO",
      r == 1 and "確認したら教えてください" in it2.get("final_text", "")
      and ledger.entry(f"{MGMT}:812.0").get("status") == "ruled"
      and ledger.entry(f"{MGMT}:811.0").get("status") is None)

# お礼のみ: 裁定なし＝何もしない
source.read_thread = mk_read([{"ts": "821.0", "user_id": runtime.TODA, "text": "助かります！"}], "820.0")
it3 = fresh_item("7.0")
r = ga["_rule_one"]({"items": {"820.0": it3}}, "820.0", it3)
check("A thanks-only -> hold", r == 0 and it3["status"] == "pending")

# ════ G: クレーム上限（投稿の恒久失敗はunactionableで終端） ════
source.read_thread = mk_read([{"ts": "831.0", "user_id": runtime.TODA, "text": "GO"}], "830.0")
source.post_thread_reply = lambda ch, ts, text: (posted.append((ch, ts, text)) or
                                                 ({"ok": True, "ts": "99.9"} if ch == MGMT else {"ok": False}))
it4 = fresh_item("8.0")
for _ in range(6):  # 6回失敗（クレームを都度失効させる）
    r = ga["_rule_one"]({"items": {"830.0": it4}}, "830.0", it4)
    check("G failed post leaves pending", r == 0 and it4["status"] == "pending")
    runtime.append_jsonl("exec_ledger.jsonl", {"id": f"{MGMT}:831.0", "at": now - 700,
                                               "status": "ruling",
                                               "tries": ledger.entry(f"{MGMT}:831.0").get("tries")})
posted.clear()
r = ga["_rule_one"]({"items": {"830.0": it4}}, "830.0", it4)
check("G retry cap -> unactionable close", r == 1 and it4["status"] == "unactionable"
      and ledger.entry(f"{MGMT}:831.0").get("status") == "ruled"
      and any("繰り返し失敗" in p[2] for p in posted))

# ════ E: 台帳の定期掃除 ════
pend = {"items": {
    "900.0": {"status": "awaiting_completion", "source_channel": "CSRC", "source_ts": "50.0"},
    "910.0": {"status": "completed", "source_channel": "CSRC", "source_ts": "60.0"},
    "920.0": {"status": "pending", "source_channel": "CSRC", "source_ts": "70.0"}}}
rows = [
    ("A1", {"owner": "apply", "status": "received", "ch": MGMT, "thread_root": "900.0", "at": now - 100}),
    ("A2", {"owner": "apply", "status": "received", "ch": "CSRC", "thread_root": "60.0", "at": now - 100}),
    ("A3", {"owner": "apply", "status": "received", "ch": MGMT, "thread_root": "920.0", "at": now - 100}),
    ("A4", {"owner": "apply", "status": "received", "ch": "CSRC", "thread_root": "50.0", "at": now - 100}),
    ("A5", {"owner": "apply", "status": "ruling", "ch": MGMT, "thread_root": "900.0", "at": now - 4000}),
    ("A6", {"owner": "apply", "status": "received", "ch": MGMT, "thread_root": "999.0", "at": now - 90000}),
    ("A7", {"owner": "intake", "status": "received", "ch": MGMT, "thread_root": "900.0", "at": now - 100}),
]
for eid, r0 in rows:
    runtime.append_jsonl("exec_ledger.jsonl", {"id": eid, **r0})
ga["_ledger_sweep"](pend)
led = ledger.load()
check("E mgmt row of non-pending item swept", led["A1"]["status"] == "skipped")
check("E source row of terminal item swept", led["A2"]["status"] == "skipped")
check("E mgmt row of pending item kept", led["A3"]["status"] == "received")
check("E source row of active tracking kept", led["A4"]["status"] == "received")
check("E stale ruling claim swept", led["A5"]["status"] == "skipped")
check("E orphan row swept after 1day", led["A6"]["status"] == "skipped")
check("E non-apply row untouched", led["A7"]["status"] == "received")

# ════ ledger-notion: ruling表示＋クレームnote除外 ════
L = f"{REPO}/profile/skills/lipple/ledger-notion/scripts/run.py"
gl = {"__file__": L, "__name__": "ln_mod"}
exec(compile(open(L).read(), L, "exec"), gl)
es = [{"id": "CX:1.0", "at": now - 60, "actor": runtime.TODA, "ch": "CX", "ts": "1.0",
       "text": "GO", "owner": "apply", "status": "received"},
      {"id": "CX:1.0", "at": now - 50, "status": "ruling", "note": "消費開始クレーム（10分で失効）"},
      {"id": "CX:1.0", "at": now - 40, "status": "ruled"}]
s = gl["summarize"](es)
check("LN ruling in transitions (jp)", "実行中" in s["trans"] and "ruling" not in s["trans"])
check("LN claim note excluded", "消費開始クレーム" not in s["note"])
s2 = gl["summarize"](es[:2])
check("LN claim-stuck outcome = 実行中", s2["outcome"] == "実行中")

# ════ C: intakeのマージ保存（lost update防止） ════
R = f"{REPO}/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)
runtime.save_json("chiaki_intake.json", {"items": {"a": {"status": "filed"}}})
mem = runtime.load_json("chiaki_intake.json", {"items": {}})  # intakeがロード（古いコピー）
runtime.save_json("chiaki_intake.json", {"items": {"a": {"status": "filed"},
                                                   "b": {"status": "awaiting_confirm"}}})  # codexが並行挿入
mem["items"]["a"]["status"] = "expired"  # intake側の変更
g["_save_items"](mem)
disk = runtime.load_json("chiaki_intake.json", {})
check("C merge keeps concurrent insert", disk["items"].get("b", {}).get("status") == "awaiting_confirm")
check("C merge keeps own change", disk["items"]["a"]["status"] == "expired")

# ════ F: intakeの再試行上限 ════
threads = {"100.0": [{"ts": "100.0", "ts_float": now - 60, "user_id": runtime.TODA, "text": "依頼"}]}
source.read_thread = lambda ch, root: threads.get(root, [])
convo.already_replied = lambda ch, ts: False
replies = []
g["_reply"] = lambda ch, root, text: replies.append((ch, root, text))
def boom(m, ch, root, items):
    raise RuntimeError("恒久故障")
g["_handle_propose"] = boom
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CX:100.0", "at": now - 700, "owner": "intake",
                                           "status": "failed", "tries": 11, "ch": "CX",
                                           "ts": "100.0", "thread_root": "100.0"})
runtime.save_json("tuning_cursor.json", {"__scan__": now})
runtime.save_json("pending_approvals.json", {"items": {}})
runtime.save_json("chiaki_intake.json", {"items": {}})
g["main"]()
e = ledger.entry("CX:100.0")
check("F 12th failure -> terminal skipped", e.get("status") == "skipped" and "再試行上限" in e.get("note", ""))
check("F give-up notice posted once", len(replies) == 1 and "完了できませんでした" in replies[0][2])
replies.clear()
g["main"]()
check("F terminated row not retried", not replies and ledger.entry("CX:100.0").get("status") == "skipped")

# ════ H: 終端済み発話の再処理ガード（走査の再発見） ════
handled_calls = []
g["_handle_propose"] = lambda m, ch, root, items: handled_calls.append(m["ts"]) or 1
threads["200.0"] = [{"ts": "200.0", "ts_float": now - 50, "user_id": runtime.TODA, "text": "処理済みのOK"}]
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CX:200.0", "at": now - 500, "owner": "intake",
                                           "status": "handled", "ch": "CX", "ts": "200.0",
                                           "thread_root": "200.0"})
g["_candidates"] = lambda cur, items: [(threads["200.0"][0], "200.0", "CX", "")]
runtime.save_json("tuning_cursor.json", {"__scan__": now - 9999})
g["main"]()
cur = runtime.load_json("tuning_cursor.json", {})
check("H handled msg not reprocessed", not handled_calls)
check("H cursor still advances", float(cur.get("CX") or 0) == now - 50)

# ════ B: codex propose = 返信が先・投稿失敗なら確認ターンを作らない ════
C = f"{REPO}/profile/skills/lipple/codex-runner/scripts/run.py"
gc = {"__file__": C, "__name__": "codex_mod"}
exec(compile(open(C).read(), C, "exec"), gc)
runtime.save_json("chiaki_intake.json", {"items": {}})
runtime.save_json("codex_threads.json", {"items": {"800.0": {
    "status": "open", "channel": "CX", "last_seen_ts": now - 100, "summary": "作業"}}})
threads["800.0"] = [{"ts": "801.0", "ts_float": now - 50, "user_id": runtime.TODA,
                     "text": "テストも改善したいね"}]
convo.decide = lambda ch, root, m, mode=None: {
    "action": "propose", "reply": "Issueとして処理しますか？",
    "proposals": [{"type": "issue", "issue_kind": "変更", "要約": "テスト改善", "詳細": "x"}]}
convo.commit = lambda: None
def creply_fail(tts, text, ch=None):
    raise RuntimeError("Slack down")
gc["_reply"] = creply_fail
gc["_process_threads"]()
items_now = runtime.load_json("chiaki_intake.json", {}).get("items", {})
t = runtime.load_json("codex_threads.json", {})["items"]["800.0"]
check("B reply-fail -> no confirm turn saved", not items_now)
check("B reply-fail -> not marked done", float(t["last_seen_ts"]) == now - 100)
sent = []
gc["_reply"] = lambda tts, text, ch=None: sent.append(text)
gc["_process_threads"]()
items_now = runtime.load_json("chiaki_intake.json", {}).get("items", {})
check("B retry -> question sent then confirm turn saved",
      len(sent) == 1 and len(items_now) == 1
      and list(items_now.values())[0]["status"] == "awaiting_confirm")

# ════ D: listenerのreceived上書きガード（該当ロジックの等価検証） ════
# listener本体はSocket Mode常駐のためロジックを直接検証: 既存行があればrecordしない
ledger.record("CY:9.0", owner="apply", status="ruled", ch="CY", ts="9.0", thread_root="8.0")
eid = "CY:9.0"
if not ledger.entry(eid).get("status"):
    ledger.record(eid, status="received")
check("D existing ruled row not downgraded", ledger.entry(eid).get("status") == "ruled")
src = open(f"{REPO}/profile/skills/lipple/event-listener/scripts/run.py").read()
check("D listener guards received overwrite",
      "if not ledger.entry(eid).get(\"status\"):" in src and "receivedで上書きしない" in src.replace(" ", ""))

print(f"\n{ok} checks passed")
