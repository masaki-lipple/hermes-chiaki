#!/usr/bin/env python3
"""R1〜R3監査（2026-07-21「このセッションでなおす！」）の6修正のテスト。
①走査カーソルの分離 ②skipped量産の根治（item既読前進・failed再試行・終端上書き禁止）
③apply系の台帳終端＋self-health監査 ④codex-runnerの効果成立後の既読化
⑤GO再実行の窓（消費開始クレーム） ⑥a台帳書き込み失敗の可視化 ⑥b裁定発話の選定。"""
import contextlib
import io
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_af"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
REPO = "/Users/malus_bot/Claude/Hermes"
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
MGMT = runtime.CH_CHIAKI_MGMT

# ════ intake（①②） ════
R = f"{REPO}/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)

runtime.save_json("pending_approvals.json", {"items": {}})
runtime.save_json("chiaki_intake.json", {"items": {}})
runtime.save_json("codex_threads.json", {"items": {}})

threads = {
    "100.0": [{"ts": "100.0", "ts_float": now - 60, "user_id": runtime.TODA,
               "text": "適正工数の件をお願いします"}],
    "200.0": [{"ts": "200.0", "ts_float": now - 50, "user_id": runtime.TODA,
               "text": "これも見ておいて"}],
    "300.0": [{"ts": "300.0", "ts_float": now - 800, "user_id": runtime.TODA, "text": "再試行対象"}],
    "400.0": [{"ts": "400.0", "ts_float": now - 200, "user_id": runtime.TODA, "text": "失敗直後"}],
    "500.0": [{"ts": "500.0", "ts_float": now - 40, "user_id": runtime.TODA, "text": "処理済みの発話"}],
}
source.read_thread = lambda ch, root: threads.get(root, [])
convo.already_replied = lambda ch, ts: False
replies = []
g["_reply"] = lambda ch, root, text: replies.append((ch, root, text))
g["_handle_propose"] = lambda m, ch, root, items: 1

# ── ① 台帳経路の処理はカーソルを進めない ──
ledger.record("CX:100.0", source="listener", actor=runtime.TODA, ch="CX", thread_root="100.0",
              ts="100.0", owner="intake", status="received")
runtime.save_json("tuning_cursor.json", {"__scan__": now})  # 走査は抑止＝台帳経路のみ
g["main"]()
cur = runtime.load_json("tuning_cursor.json", {})
check("① ledger-path handled", ledger.entry("CX:100.0").get("status") == "handled")
check("① ledger-path does not advance cursor", "CX" not in cur)

# ── ① 走査由来の候補はカーソルを進める（成功時） ──
def scan_returns(msgs):
    def _scan(cur, items):
        return [(m, m["ts"], "CX", "") for m in msgs]
    return _scan
g["_candidates"] = scan_returns([threads["200.0"][0]])
runtime.save_json("tuning_cursor.json", {"__scan__": now - 9999})
g["main"]()
cur = runtime.load_json("tuning_cursor.json", {})
check("① scan-path advances cursor", float(cur.get("CX") or 0) == now - 50)
check("① scan-path handled", ledger.entry("CX:200.0").get("status") == "handled")

# ── ①② 失敗＝走査カーソルは進める（再試行は台帳failedが担う）＋初回だけ決定論の通知 ──
def boom(m, ch, root, items):
    raise RuntimeError("LLM全滅")
g["_handle_propose"] = boom
g["_candidates"] = scan_returns([{"ts": "210.0", "ts_float": now - 45, "user_id": runtime.TODA,
                                  "text": "失敗する依頼"}])
threads["210.0"] = [{"ts": "210.0", "ts_float": now - 45, "user_id": runtime.TODA,
                     "text": "失敗する依頼"}]
runtime.save_json("tuning_cursor.json", {"__scan__": now - 9999})
replies.clear()
g["main"]()
cur = runtime.load_json("tuning_cursor.json", {})
check("① failure still advances scan cursor", float(cur.get("CX") or 0) == now - 45)
check("② failure recorded as failed", ledger.entry("CX:210.0").get("status") == "failed")
check("② first failure -> honest notice", len(replies) == 1 and "不調" in replies[0][2])

# ── ②b failed行の再試行は台帳経路（10分バックオフ） ──
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CX:300.0", "at": now - 800, "owner": "intake",
                                           "status": "failed", "ch": "CX", "ts": "300.0",
                                           "thread_root": "300.0"})
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CX:400.0", "at": now - 200, "owner": "intake",
                                           "status": "failed", "ch": "CX", "ts": "400.0",
                                           "thread_root": "400.0"})
cand = g["_ledger_candidates"]({})
got = {c[0]["ts"] for c in cand}
check("②b failed row retried after 10min", "300.0" in got)
check("②b fresh failed row backs off", "400.0" not in got)

# ── ② already_replied: 終端状態を上書きしない＋item既読前進＋走査カーソル前進 ──
ledger.record("CX:500.0", ch="CX", thread_root="500.0", ts="500.0",
              owner="intake", status="handled")  # 別経路が正常処理済み（終端）
runtime.save_json("chiaki_intake.json", {"items": {"90.0": {
    "status": "awaiting_confirm", "channel": "CX", "thread_root": "500.0",
    "last_seen_ts": "90.0", "proposed_at": now}}})
convo.already_replied = lambda ch, ts: ts == "500.0"
g["_handle_propose"] = lambda m, ch, root, items: 1
g["_candidates"] = scan_returns([threads["500.0"][0]])
runtime.save_json("tuning_cursor.json", {"__scan__": now - 9999})
g["main"]()
check("② terminal status not overwritten by skipped",
      ledger.entry("CX:500.0").get("status") == "handled")
items_after = runtime.load_json("chiaki_intake.json", {}).get("items", {})
check("②a item last_seen advanced past skipped msg",
      items_after.get("90.0", {}).get("last_seen_ts") == "500.0")
cur = runtime.load_json("tuning_cursor.json", {})
check("② skip branch advances scan cursor", float(cur.get("CX") or 0) == now - 40)

# received のままの行は skipped で終端される
ledger.record("CX:510.0", ch="CX", thread_root="510.0", ts="510.0",
              owner="intake", status="received")
threads["510.0"] = [{"ts": "510.0", "ts_float": now - 30, "user_id": runtime.TODA, "text": "x"}]
convo.already_replied = lambda ch, ts: ts == "510.0"
g["_candidates"] = scan_returns([threads["510.0"][0]])
runtime.save_json("tuning_cursor.json", {"__scan__": now - 9999})
g["main"]()
e = ledger.entry("CX:510.0")
check("② received row terminated as skipped on already_replied",
      e.get("status") == "skipped" and e.get("note") == "already_replied")
convo.already_replied = lambda ch, ts: False

# ── ②a _advance_item_seen 単体 ──
its = {"a": {"status": "awaiting_confirm", "channel": "C1", "thread_root": "10.0", "last_seen_ts": "5.0"},
       "b": {"status": "filed", "channel": "C1", "thread_root": "10.0", "last_seen_ts": "20.0"},
       "c": {"status": "expired", "channel": "C1", "thread_root": "10.0", "last_seen_ts": "5.0"}}
g["_advance_item_seen"](its, "C1", "10.0", "15.0")
check("②a advance only forward + only awaiting/filed",
      its["a"]["last_seen_ts"] == "15.0" and its["b"]["last_seen_ts"] == "20.0"
      and its["c"]["last_seen_ts"] == "5.0")

# ════ codex-runner（④） ════
C = f"{REPO}/profile/skills/lipple/codex-runner/scripts/run.py"
gc = {"__file__": C, "__name__": "codex_mod"}
exec(compile(open(C).read(), C, "exec"), gc)
runtime.save_json("chiaki_intake.json", {"items": {}})
commits = {"n": 0}
convo.commit = lambda: commits.update(n=commits["n"] + 1)

def codex_thread(last_seen):
    runtime.save_json("codex_threads.json", {"items": {"800.0": {
        "status": "open", "channel": "CX", "last_seen_ts": last_seen, "summary": "テスト作業"}}})

# ── ④ 返信投稿の失敗＝既読化しない（次回リトライ）・台帳にも書かない ──
threads["800.0"] = [{"ts": "801.0", "ts_float": now - 50, "user_id": runtime.TODA, "text": "質問です"}]
codex_thread(now - 100)
convo.decide = lambda ch, root, m, mode=None: {"action": "answer", "reply": "お答えします！"}
def creply_fail(tts, text, ch=None):
    raise RuntimeError("Slack down")
gc["_reply"] = creply_fail
gc["_process_threads"]()
t = runtime.load_json("codex_threads.json", {})["items"]["800.0"]
check("④ reply failure -> last_seen not advanced", float(t["last_seen_ts"]) == now - 100)
check("④ reply failure -> no ledger terminal", ledger.entry("CX:801.0").get("status") is None)
check("④ reply failure -> convo not committed", commits["n"] == 0)

# ── ④ 成功＝既読化・台帳replied・会話台帳commit ──
creplies = []
gc["_reply"] = lambda tts, text, ch=None: creplies.append((tts, text))
gc["_process_threads"]()
t = runtime.load_json("codex_threads.json", {})["items"]["800.0"]
check("④ retry succeeds -> replied once", len(creplies) == 1)
check("④ success -> last_seen advanced", float(t["last_seen_ts"]) == now - 50)
check("④ success -> ledger replied", ledger.entry("CX:801.0").get("status") == "replied")
check("④ success -> convo committed", commits["n"] == 1)

# ── ④ continue: キュー投入（本質的効果）成立で既読化＝返信失敗でも二重投入しない ──
threads["800.0"].append({"ts": "802.0", "ts_float": now - 40, "user_id": runtime.TODA,
                         "text": "テストも追加して"})
convo.decide = lambda ch, root, m, mode=None: {"action": "codex_continue",
                                               "reply": "着手します！", "instruction": "テスト追加"}
gc["_reply"] = creply_fail
gc["_process_threads"]()
t = runtime.load_json("codex_threads.json", {})["items"]["800.0"]
q = list(runtime.read_jsonl("codex_queue.jsonl"))
check("④ continue queued despite reply failure", len(q) == 1 and q[0]["detail"] == "テスト追加")
check("④ continue -> last_seen advanced (no requeue loop)", float(t["last_seen_ts"]) == now - 40)
check("④ continue -> ledger queued", ledger.entry("CX:802.0").get("status") == "queued")
gc["_process_threads"]()
check("④ no reprocess after done", len(list(runtime.read_jsonl("codex_queue.jsonl"))) == 1)

# ════ apply-ruling（⑤⑥b③） ════
A = f"{REPO}/profile/skills/lipple/apply-ruling/scripts/run.py"
ga = {"__file__": A, "__name__": "apply_mod"}
exec(compile(open(A).read(), A, "exec"), ga)
posted = []
source.post_thread_reply = lambda ch, ts, text: posted.append((ch, ts, text)) or {"ok": True, "ts": "99.9"}

def fresh_item(src_ts):
    return {"status": "pending", "source_channel": "CSRC", "source_ts": src_ts,
            "target_user_id": "U09T44VEZM1", "draft": "修正をお願いします！",
            "verify_found": "ですです"}

# ── ⑥b GOの後に「ありがとう」が続いても、GOが裁定発話として選ばれる ──
def fake_read(ch, root):
    if ch == MGMT and root == "900.0":
        return [{"ts": "900.0", "user_id": runtime.CHIAKI_SELF, "text": "提案"},
                {"ts": "901.0", "user_id": runtime.TODA, "text": "GO"},
                {"ts": "902.0", "user_id": runtime.TODA, "text": "ありがとう！"}]
    return [{"ts": root, "user_id": "U09T44VEZM1", "text": "本文ですです"}]
source.read_thread = fake_read
it = fresh_item("5.0")
r = ga["_rule_one"]({"items": {"900.0": it}}, "900.0", it)
led = ledger.entry(f"{MGMT}:901.0")
check("⑥b GO found behind later thanks", r == 1 and it.get("status") == "awaiting_completion")
check("⑥b binding bound to GO ts (not newest)", led.get("status") == "ruled"
      and ledger.entry(f"{MGMT}:902.0").get("status") is None)
posted.clear()
it2 = fresh_item("5.0")
r = ga["_rule_one"]({"items": {"900.0": it2}}, "900.0", it2)
check("⑥b idempotent on ruled GO", r == 0 and not posted)

# 「ありがとう」だけのスレッドは裁定なし＝何もしない
def fake_read_thanks(ch, root):
    if ch == MGMT:
        return [{"ts": "910.0", "user_id": runtime.CHIAKI_SELF, "text": "提案"},
                {"ts": "911.0", "user_id": runtime.TODA, "text": "ありがとう！"}]
    return [{"ts": root, "user_id": "U09T44VEZM1", "text": "本文ですです"}]
source.read_thread = fake_read_thanks
it3 = fresh_item("5.0")
r = ga["_rule_one"]({"items": {"910.0": it3}}, "910.0", it3)
check("⑥b thanks-only -> no ruling", r == 0 and it3["status"] == "pending"
      and ledger.entry(f"{MGMT}:911.0").get("status") is None)

# ── ⑤ 消費開始クレーム: 投稿失敗（クラッシュ相当）でもクレームが残り、10分は再実行しない ──
def fake_read_go(ch, root):
    if ch == MGMT:
        return [{"ts": "920.0", "user_id": runtime.CHIAKI_SELF, "text": "提案"},
                {"ts": "921.0", "user_id": runtime.TODA, "text": "GO"}]
    return [{"ts": root, "user_id": "U09T44VEZM1", "text": "本文ですです"}]
source.read_thread = fake_read_go
def post_crash(ch, ts, text):
    raise RuntimeError("crash between post and save")
source.post_thread_reply = post_crash
it4 = fresh_item("6.0")
try:
    ga["_rule_one"]({"items": {"920.0": it4}}, "920.0", it4)
    crashed = False
except RuntimeError:
    crashed = True
eid = f"{MGMT}:921.0"
check("⑤ claim recorded before target post", crashed and ledger.entry(eid).get("status") == "ruling")
posted.clear()
source.post_thread_reply = lambda ch, ts, text: posted.append((ch, ts, text)) or {"ok": True, "ts": "99.9"}
it5 = fresh_item("6.0")
r = ga["_rule_one"]({"items": {"920.0": it5}}, "920.0", it5)
check("⑤ fresh claim blocks re-execution", r == 0 and not posted)
runtime.append_jsonl("exec_ledger.jsonl", {"id": eid, "at": now - 700, "status": "ruling"})
it6 = fresh_item("6.0")
r = ga["_rule_one"]({"items": {"920.0": it6}}, "920.0", it6)
check("⑤ expired claim -> retried and ruled", r == 1 and len(posted) == 2
      and ledger.entry(eid).get("status") == "ruled")

# ── ③ _ledger_close_thread: apply所有のreceived行だけ終端 ──
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CSRC:7.1", "at": now - 100, "owner": "apply",
                                           "status": "received", "ch": "CSRC", "ts": "7.1",
                                           "thread_root": "7.0"})
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CSRC:7.2", "at": now - 90, "owner": "apply",
                                           "status": "received", "ch": "CSRC", "ts": "7.2",
                                           "thread_root": "7.0"})
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CSRC:7.3", "at": now - 80, "owner": "intake",
                                           "status": "received", "ch": "CSRC", "ts": "7.3",
                                           "thread_root": "7.0"})
ga["_ledger_close_thread"]("CSRC", "7.0", "テスト終端")
check("③ close_thread terminates apply rows",
      ledger.entry("CSRC:7.1").get("status") == "skipped"
      and ledger.entry("CSRC:7.2").get("status") == "skipped")
check("③ close_thread leaves other owners", ledger.entry("CSRC:7.3").get("status") == "received")

# ════ self-health（③） ════
H = f"{REPO}/profile/skills/lipple/self-health/scripts/run.py"
gh = {"__file__": H, "__name__": "health_mod"}
exec(compile(open(H).read(), H, "exec"), gh)
runtime.save_json("pending_approvals.json", {"items": {
    "t1": {"status": "awaiting_completion", "source_ts": "70.0"},
    "t2": {"status": "completed", "source_ts": "80.0"}}})
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CSRC:70.1", "at": now - 80 * 3600, "owner": "apply",
                                           "status": "received", "ch": "CSRC", "ts": "70.1",
                                           "thread_root": "70.0"})  # 追跡中+3日超=警告
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CSRC:70.2", "at": now - 3600, "owner": "apply",
                                           "status": "received", "ch": "CSRC", "ts": "70.2",
                                           "thread_root": "70.0"})  # 新しい=対象外
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CSRC:80.1", "at": now - 80 * 3600, "owner": "apply",
                                           "status": "received", "ch": "CSRC", "ts": "80.1",
                                           "thread_root": "80.0"})  # 追跡外(completed)=対象外
warns = gh["_apply_stale"](now)
check("③ apply_stale warns only tracked+old rows",
      len(warns) == 1 and "70.1" in warns[0])

# ════ ⑥a ledger.record は書けなくても無音にしない ════
orig_append = runtime.append_jsonl
def append_fail(name, row):
    raise OSError("disk full")
runtime.append_jsonl = append_fail
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    ledger.record("CX:err.0", status="handled")
runtime.append_jsonl = orig_append
check("⑥a record failure printed (not silent)", "[ledger] record失敗 CX:err.0" in buf.getvalue())

print(f"\n{ok} checks passed")
