#!/usr/bin/env python3
"""Codex起動の安全化3点（2026-08-07 戸田「イシューに追加するだけでいいのにCodexまわしはじめた、
これ危険すぎる」「Yu MatsunagaへのメンションをなぜかChiaki AIが返してくる」）のテスト。
①起票のみが既定＝Codex起動は戸田さんの実文の明示（Codex/実装して等）があるときだけ
②Codex登録スレッド内の戸田→他メンバー宛（@松永等）にChiaki AIが割り込まない
③retractで未実行のCodexキューを取り消す（「まわさなくていい」がキューに効く）。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_cg"
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

from lib import convo, ledger, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

now = runtime.now_ts()

# ════ ① Codex起動は明示指示だけ（決定論ゲート） ════
R = f"{REPO}/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)
check("① explicit trigger matches", bool(g["_CODEX_GO_RE"].search("これCodexに回して")))
check("① 実装して matches", bool(g["_CODEX_GO_RE"].search("そのまま実装して進めて")))
check("① いったんイシューに は明示でない",
      not g["_CODEX_GO_RE"].search("いったんイシューにいれておこう。"))
check("① OKだけも明示でない", not g["_CODEX_GO_RE"].search("OK！お願いします。"))

# file経路: 明示なし=キュー投入なし・明示あり=投入
posted = []
g["_reply"] = lambda ch, root, text, url="": posted.append(text)
g["_file_issue"] = lambda p, permalink, ch: "http://issue/1"
g["_maybe_edit_root"] = lambda *a, **k: "norevise"
g["_handle_go_extra"] = lambda *a, **k: None
convo.commit = lambda: None

def run_file(user_text, agent_codex=True):
    (SCRATCH / "state" / "codex_queue.jsonl").unlink(missing_ok=True)
    it = {"status": "awaiting_confirm", "channel": "CX", "thread_root": "10.0",
          "permalink": "http://x", "mention_text": "この機能を追加したい",
          "proposals": [{"type": "issue", "issue_kind": "変更", "要約": "y", "詳細": "z"}]}
    convo.decide = lambda ch, root, m, mode=None, extra_facts=None: {
        "action": "file", "reply": "登録しました！", "codex": agent_codex, "proposals": []}
    m = {"ts": "11.0", "ts_float": now, "user_id": runtime.TODA, "text": user_text}
    g["_handle_confirm"](it, m, "CX", "10.0")
    return list(runtime.read_jsonl("codex_queue.jsonl")), it

q, it = run_file("いったんイシューにいれておこう。", agent_codex=True)
check("① soft phrase -> filed only (no queue despite agent codex=true)",
      not q and it["status"] == "filed")
q, it = run_file("OK、Codexに回して実装まで進めて！", agent_codex=True)
check("① explicit -> queued", len(q) == 1 and it["status"] == "filed")

# ════ ② Codexスレッド内の他メンバー宛スキップ ════
C = f"{REPO}/profile/skills/lipple/codex-runner/scripts/run.py"
gc = {"__file__": C, "__name__": "codex_mod"}
exec(compile(open(C).read(), C, "exec"), gc)
runtime.save_json("chiaki_intake.json", {"items": {}})
runtime.save_json("codex_threads.json", {"items": {"800.0": {
    "status": "open", "channel": "CX", "last_seen_ts": now - 100, "summary": "作業"}}})
threads = {"800.0": [
    {"ts": "801.0", "ts_float": now - 50, "user_id": runtime.TODA,
     "text": "<@U09T44VEZM1> できた？"},
    {"ts": "802.0", "ts_float": now - 40, "user_id": runtime.TODA,
     "text": "<@U09T44VEZM1> あ、これ前半のほうのサブタスクってイメージ？"}]}
source.read_thread = lambda ch, root: threads.get(root, [])
convo.already_replied = lambda ch, ts: False
decided = []
convo.decide = lambda ch, root, m, mode=None, extra_facts=None: decided.append(m) or None
gc["_process_threads"]()
t = runtime.load_json("codex_threads.json", {})["items"]["800.0"]
check("② toda->matsunaga messages skipped (no convo call)",
      not decided and float(t["last_seen_ts"]) == now - 40)
check("② skip recorded in ledger",
      ledger.entry("CX:802.0").get("note") == "宛先が他メンバー")

# Chiaki宛が混ざれば、それだけ処理される
threads["800.0"].append({"ts": "803.0", "ts_float": now - 30, "user_id": runtime.TODA,
                         "text": f"<@{runtime.CHIAKI_SELF}> これ進めて"})
replies = []
gc["_reply"] = lambda tts, text, ch=None: replies.append(text)
convo.decide = lambda ch, root, m, mode=None, extra_facts=None: {
    "action": "answer", "reply": "進めます、承知しました。"}
convo.commit = lambda: None
gc["_process_threads"]()
check("② chiaki-addressed message still handled", len(replies) == 1)

# listener側の判定ヘルパ
L = f"{REPO}/profile/skills/lipple/event-listener/scripts/run.py"
src = open(L).read()
check("② listener guards codex route",
      "_mentions_only_others(ev.get(\"text\")" in src.replace("'", '"'))
import re as _re
ns = {"re": _re, "runtime": runtime}
fn_src = "def _mentions_only_others" + src.split("def _mentions_only_others")[1].split("\nWATCH_MGMT")[0]
exec(fn_src, ns)
check("② only-others detection", ns["_mentions_only_others"]("<@U09T44VEZM1> できた？")
      and not ns["_mentions_only_others"](f"<@{runtime.CHIAKI_SELF}> これ進めて")
      and not ns["_mentions_only_others"]("メンション無しの返信"))

# ════ ②b intakeの全経路ガード（確認ターン経由の干渉の再発防止・2026-08-07「また干渉してくる」） ════
g3 = {"__file__": R, "__name__": "intake_mod3"}
exec(compile(open(R).read(), R, "exec"), g3)
runtime.save_json("pending_approvals.json", {"items": {}})
runtime.save_json("chiaki_intake.json", {"items": {"70.0": {
    "status": "awaiting_confirm", "channel": "CY", "thread_root": "70.0",
    "last_seen_ts": "70.0", "proposed_at": now,
    "proposals": [{"type": "issue", "要約": "x"}]}}})
threads_g3 = {"70.0": [
    {"ts": "70.0", "ts_float": now - 300, "user_id": runtime.CHIAKI_SELF, "text": "Issueとして処理しますか？"},
    {"ts": "71.0", "ts_float": now - 50, "user_id": runtime.TODA,
     "text": "<@U09T44VEZM1> こっちもですね！これ先方サーバーなのできいてみます！"}]}
source.read_thread = lambda ch, root: threads_g3.get(root, [])
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CY:71.0", "at": now - 40, "owner": "intake",
                                           "status": "received", "ch": "CY", "ts": "71.0",
                                           "thread_root": "70.0"})
runtime.save_json("tuning_cursor.json", {"__scan__": now})
handled = []
g3["_handle_confirm"] = lambda it, m, ch, root: handled.append(m["ts"]) or 1
g3["_handle_propose"] = lambda m, ch, root, items: handled.append(m["ts"]) or 1
convo.already_replied = lambda ch, ts: False
g3["main"]()
e = ledger.entry("CY:71.0")
items_after = runtime.load_json("chiaki_intake.json", {}).get("items", {})
check("②b intake skips toda->others even in confirm turn",
      not handled and e.get("status") == "skipped" and e.get("note") == "宛先が他メンバー")
check("②b confirm-turn last_seen advanced (no rediscover loop)",
      items_after["70.0"]["last_seen_ts"] == "71.0")

# ════ ③ retractでキュー取り消し ════
(SCRATCH / "state" / "codex_queue.jsonl").unlink(missing_ok=True)  # ①の残りを掃除＝対象を単離
runtime.append_jsonl("codex_queue.jsonl", {"ts": str(now - 60), "requested_by": runtime.TODA,
                                           "summary": "s", "detail": "d", "issue_url": "",
                                           "channel": "CX", "thread": "900.0"})
# intakeのretractがcancel行を書く
g2 = {"__file__": R, "__name__": "intake_mod2"}
exec(compile(open(R).read(), R, "exec"), g2)
g2["_reply"] = lambda ch, root, text, url="", gate=True: None
source.read_thread = lambda ch, root: [{"ts": "901.0", "user_id": runtime.CHIAKI_SELF,
                                        "text": "Codexが作業を開始しました。"}]
runtime.save_json("pending_approvals.json", {"items": {}})
fake_llm.gpt = lambda *a, **k: json.dumps({"mistake": True, "reply": "取り消しました。"},
                                          ensure_ascii=False)
source.update_message = lambda ch, ts, text: {"ok": True}
g2["_handle_retract"]({"text": "いやCodexまわさなくていい"}, "CX", "900.0")
cancels = runtime.read_jsonl("codex_cancel.jsonl")
check("③ retract records cancel row", len(cancels) == 1 and cancels[0]["thread"] == "900.0")

# runnerが実行前に取り消しを反映
cancel_note = []
source.post_thread_reply = lambda ch, ts, text: cancel_note.append(text) or {"ok": True, "ts": "9.9"}
gc2 = {"__file__": C, "__name__": "codex_mod2"}
exec(compile(open(C).read(), C, "exec"), gc2)
gc2["_process_threads"] = lambda: None
gc2["main"]()
st = runtime.load_json("codex_runner.json", {})
check("③ runner cancels queued item before run",
      st["done"].get(str(now - 60), {}).get("status") == "cancelled"
      and any("取り消された" in t for t in cancel_note))

print(f"\n{ok} checks passed")
