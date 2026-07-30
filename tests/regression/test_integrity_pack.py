#!/usr/bin/env python3
"""整合パック11〜15（2026-07-29 戸田「A. 発話と実行の整合 これは全部やろう」）のテスト。
11:発話ゲート 12:成立確認（編集・取り消し・返信ts） 13:出口台帳 14:約束台帳 15:日次発話整合監査。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_ip"
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

now = runtime.now_ts()
MGMT = runtime.CH_CHIAKI_MGMT

# ════ 11: 発話ゲート（lib/convo.claims_gate） ════
LIE = "承知しました。Issueに登録し、Codexに回します。進捗はこのスレッドに返します！"
kept, dropped = convo.claims_gate(LIE, set())
check("11 unbacked claims dropped", len(dropped) == 2 and "承知しました。" in kept
      and "登録" not in kept and "進捗" not in kept)
kept, dropped = convo.claims_gate(LIE, {"filed", "queued"})
check("11 backed claims kept", not dropped and "Codexに回します" in kept)
kept, dropped = convo.claims_gate("この内容でIssueとして処理しますか？", set())
check("11 questions exempt", not dropped and "Issueとして処理しますか？" in kept)
kept, dropped = convo.claims_gate("登録しました！\nhttps://x", {"filed"})
check("11 filed claim with effect kept", not dropped)
kept, dropped = convo.claims_gate("確認して改めて返します！", set())
check("11 executor-less promise dropped", dropped and not kept)
kept, dropped = convo.claims_gate("復旧し次第このスレッドに改めて返信します！", {"retry_scheduled"})
check("11 retry-backed promise kept", not dropped)
kept, dropped = convo.claims_gate("さきほどの投稿は取り消しました。", set())
check("11 retract claim needs effect", len(dropped) == 1)

# 過去形の主張は「記録の裏付け」でも通る（2026-07-29 敵対検証: 正当な過去事実の回答まで検閲していた）
runtime.save_json("chiaki_intake.json", {"items": {"a": {
    "thread_root": "77.0", "status": "filed", "page_urls": ["http://x"]}}})
kept, dropped = convo.claims_gate("こちらは既に登録済みです！", set(), MGMT, "77.0")
check("11 past claim passes with filed evidence", not dropped)
kept, dropped = convo.claims_gate("こちらは既に登録済みです！", set(), MGMT, "99.0")
check("11 past claim without evidence dropped", dropped)
kept, dropped = convo.claims_gate("Issueに登録します。", set(), MGMT, "77.0")
check("11 future promise still strict (evidence not enough)", dropped)
runtime.save_json("pending_approvals.json", {"items": {"700.0": {
    "status": "awaiting_completion", "source_ts": "5.0", "nudge_ts": "701.0"}}})
kept, dropped = convo.claims_gate("記録では対象スレッドへ修正依頼を投稿しました。", set(), MGMT, "700.0")
check("11 posted claim passes with nudge evidence", not dropped)
runtime.save_json("chiaki_intake.json", {"items": {}})
runtime.save_json("pending_approvals.json", {"items": {}})

# ════ intake: _replyのゲート適用・空文フォールバック・ts検証（12） ════
R = f"{REPO}/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)
posted = []
source.post_thread_reply = lambda ch, root, text: posted.append(text) or {"ok": True, "ts": "1.0"}
g["_TURN_EFFECTS"].clear()
g["_reply"]("CX", "1.0", LIE)
check("11 intake reply gated", "登録" not in posted[-1] and "承知しました" in posted[-1])
g["_reply"]("CX", "1.0", "Issueに登録します。")
check("11 all-dropped -> honest fallback", "まとめられませんでした" in posted[-1])
g["_effect"]("filed")
g["_reply"]("CX", "1.0", "登録しました！")
check("11 effect unlocks claim", "登録しました" in posted[-1])
g["_TURN_EFFECTS"].clear()
source.post_thread_reply = lambda ch, root, text: posted.append(text) or {"ok": True, "ts": "1.0"}
g["_reply"]("CX", "1.0", "内容を確認して、Codexに回します！", gate=False)
check("11 progress-watch direction exempt (gate=False)", "Codexに回します" in posted[-1])
source.post_thread_reply = lambda ch, root, text: {"ok": False}
try:
    g["_reply"]("CX", "1.0", "テスト")
    raised = False
except RuntimeError:
    raised = True
check("12 reply without ts raises (retryable)", raised)

# _await=提示文の投稿が先（投稿失敗で幽霊awaitingを作らない・2026-07-29 敵対検証）
items_t = {}
try:
    g["_await"](items_t, {"ts": "50.0", "text": "依頼"}, "CX", "50.0",
                [{"type": "issue", "要約": "x", "詳細": "y"}])
    await_raised = False
except RuntimeError:
    await_raised = True
check("12 await posts before registering (no ghost)", await_raised and not items_t)

# ════ 12: _edit_post の成立確認 ════
source.read_thread = lambda ch, root: [{"ts": "5.0", "user_id": runtime.CHIAKI_SELF,
                                        "text": "・箇条書きです。"}]  # enforceで「• 」に直る=決定論編集が走る
source.update_message = lambda ch, ts, text: {"ok": False}
check("12 edit failure -> editfail", g["_edit_post"]("CX", "5.0", "5.0", "直して") == "editfail")
g["_TURN_EFFECTS"].clear()
source.update_message = lambda ch, ts, text: {"ok": True}
check("12 edit success -> edited + effect",
      g["_edit_post"]("CX", "5.0", "5.0", "直して") == "edited" and "edited" in g["_TURN_EFFECTS"])
check("12 editfail message exists", "editfail" in g["_EDIT_MSG"])

# ════ 12: retractの成立確認（注記が書けなければ取り消し宣言しない・裁定も閉じない） ════
replies = []
g["_reply"] = lambda ch, root, text: replies.append(text)
g["_TURN_EFFECTS"].clear()
THREAD = [{"ts": "900.0", "user_id": runtime.CHIAKI_SELF, "user_name": "Chiaki AI",
           "text": "対象スレッドへ投稿しました。"}]
source.read_thread = lambda ch, root: THREAD
runtime.save_json("pending_approvals.json", {"items": {"800.0": {
    "status": "awaiting_completion", "source_channel": "CX", "source_ts": "900.0"}}})
fake_llm.gpt = lambda *a, **k: json.dumps({"mistake": True, "reply": "失礼しました！取り消しました。"},
                                          ensure_ascii=False)
source.update_message = lambda ch, ts, text: {"ok": False}
r = g["_handle_retract"]({"text": "これ間違いだよ"}, "CX", "900.0")
pend = runtime.load_json("pending_approvals.json", {})
check("12 retract annotation failure -> honest + not closed",
      r == 1 and "失敗" in replies[-1]
      and pend["items"]["800.0"]["status"] == "awaiting_completion")
source.update_message = lambda ch, ts, text: {"ok": True}
r = g["_handle_retract"]({"text": "これ間違いだよ"}, "CX", "900.0")
pend = runtime.load_json("pending_approvals.json", {})
check("12 retract success -> closed + effect",
      pend["items"]["800.0"]["status"] == "retracted" and "retracted" in g["_TURN_EFFECTS"])

# 注記済みの投稿には多重追記しない＋最終返信の失敗で例外を上げない（2026-07-29 敵対検証）
updates = []
source.update_message = lambda ch, ts, text: updates.append(text) or {"ok": True}
THREAD[0]["text"] = "対象スレッドへ投稿しました。\n\n※この投稿は誤りでした。取り消します。"
runtime.save_json("pending_approvals.json", {"items": {"800.0": {
    "status": "awaiting_completion", "source_channel": "CX", "source_ts": "900.0"}}})
def reply_fail(ch, root, text):
    raise RuntimeError("Slack down")
g["_reply"] = reply_fail
r = g["_handle_retract"]({"text": "これ間違いだよ"}, "CX", "900.0")
pend = runtime.load_json("pending_approvals.json", {})
check("12 annotated post not re-annotated + reply failure contained",
      r == 1 and not updates and pend["items"]["800.0"]["status"] == "retracted")
g["_reply"] = lambda ch, root, text: replies.append(text)

# ════ 13: 出口台帳 ════
source._out_track("reply", "CX", {"ok": True, "ts": "9.0"}, thread="1.0", text="本文テスト")
rows = runtime.read_jsonl("out_ledger.jsonl")
check("13 out ledger row", len(rows) == 1 and rows[0]["kind"] == "reply" and rows[0]["ok"]
      and rows[0]["ts"] == "9.0" and rows[0]["caller"] == "test_integrity_pack"
      and rows[0]["head"] == "本文テスト")
source._out_track("post", "CX", {"ok": False}, text="失敗分")
check("13 failure tracked", runtime.read_jsonl("out_ledger.jsonl")[-1]["ok"] is False)

# ════ 14: 約束台帳（発行・履行・self-health監査） ════
C = f"{REPO}/profile/skills/lipple/codex-runner/scripts/run.py"
gc = {"__file__": C, "__name__": "codex_mod"}
exec(compile(open(C).read(), C, "exec"), gc)
gc["_fulfill"]("CX", "70.0")                 # 過去の履行（約束より前）＝新しい約束をマスクしない
gc["_promise"]("CX", "70.0", due_hours=-1)   # すでに期限切れの約束
gc["_promise"]("CX", "80.0", due_hours=-1)
gc["_fulfill"]("CX", "80.0")                 # 80.0 は約束の後に履行済み
gc["_promise"]("CX", "90.0", due_hours=24)   # 90.0 は期限内
H = f"{REPO}/profile/skills/lipple/self-health/scripts/run.py"
gh = {"__file__": H, "__name__": "health_mod"}
exec(compile(open(H).read(), H, "exec"), gh)
warns = gh["_promises_broken"](runtime.now_ts())
check("14 overdue unfulfilled warned (old fulfill does not mask)",
      len(warns) == 1 and "70.0" in warns[0])
check("14 warned row deferred until post succeeds",
      not any(r.get("kind") == "warned" for r in runtime.read_jsonl("promises.jsonl"))
      and len(gh["_PENDING_WARNED"]) == 1)
runtime.append_jsonl("promises.jsonl", {**gh["_PENDING_WARNED"][0], "at": runtime.now_ts()})
gh["_PENDING_WARNED"].clear()  # mainの投稿成立後の記録を再現
check("14 warn idempotent after recorded", gh["_promises_broken"](runtime.now_ts()) == [])

# intakeのCodexキュー投入で約束が発行される
(SCRATCH / "state" / "promises.jsonl").unlink()
g["_TURN_EFFECTS"].clear()
note = g["_maybe_enqueue_codex"]({"mention_text": ""}, {"user_id": runtime.TODA, "text": "x"},
                                 "CX", "60.0", [({"type": "issue", "要約": "a"}, "http://i")],
                                 force=True)
promises = runtime.read_jsonl("promises.jsonl")
check("14 intake enqueue -> promise + queued effect",
      "進捗" in note and "queued" in g["_TURN_EFFECTS"]
      and len(promises) == 1 and promises[0]["kind"] == "codex_report" and promises[0]["root"] == "60.0")

# ════ 15: 日次サマリの発話整合監査 ════
D = f"{REPO}/profile/skills/lipple/daily-summary/scripts/run.py"
gd = {"__file__": D, "__name__": "ds_mod"}
exec(compile(open(D).read(), D, "exec"), gd)
runtime.save_json(convo.MEM_FILE, {"ledger": [
    {"ts": now, "dt": "07-29 10:00", "ch": MGMT, "root": "1.0", "action": "answer",
     "reply": "Issueに登録し、Codexに回します。"},  # ゲートが防ぐ側＝監査では誤報にしない
    {"ts": now, "dt": "07-29 10:05", "ch": MGMT, "root": "2.0", "action": "file",
     "reply": "登録しました！"},
    {"ts": now, "dt": "07-29 10:06", "ch": MGMT, "root": "3.0", "action": "file",
     "reply": "登録しました！"}]})
runtime.save_json("chiaki_intake.json", {"items": {
    "a": {"thread_root": "2.0", "status": "filed", "page_urls": ["http://x"]}}})
text = gd["build"](now)
check("15 integrity line: file without filing flagged only",
      "発話整合: 要確認1件" in text and "起票URLの裏付けなし" in text
      and "answerに実行主張" not in text)
runtime.save_json(convo.MEM_FILE, {"ledger": []})
check("15 clean day -> no line", "発話整合" not in gd["build"](now))

print(f"\n{ok} checks passed")
