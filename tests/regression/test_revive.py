#!/usr/bin/env python3
"""失効提案の復活（2026-08-07 戸田「これも指摘で！」＝失効通知は「もう一度お知らせください」と
案内するのに復活の仕組みが無かった）のテスト。intakeが復活の印を付け、apply-rulingの通常GO経路
（クレーム・生存/修正済み確認・バインディング・成立確認）が実行する。"""
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_rv"
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

from lib import ledger, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

now = runtime.now_ts()
MGMT = runtime.CH_CHIAKI_MGMT

R = f"{REPO}/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)
replies = []
g["_reply"] = lambda ch, root, text, url="", gate=True: replies.append(text)

def pend_item(status="expired"):
    return {"items": {"900.0": {
        "finding_kind": "typo", "status": status, "source_channel": "CSRC", "source_ts": "5.0",
        "target_user_id": "U09T44VEZM1", "target_name": "Yu Matsunaga",
        "draft": "「2026年07月128日」を「2026年07月28日」に修正お願いします！",
        "verify_found": "2026年07月128日"}}}

# ── intake: 失効スレッドでの前向き指示=pendingへ戻しrevive印 ──
runtime.save_json("pending_approvals.json", pend_item())
m = {"ts": "950.0", "ts_float": now, "user_id": runtime.TODA,
     "text": "<@U0BCCMPKD54>\nこれも指摘で！"}
r = g["_maybe_revive"](m, MGMT, "900.0")
pend = runtime.load_json("pending_approvals.json", {})
it = pend["items"]["900.0"]
check("revive: expired -> pending with mark", r == 1 and it["status"] == "pending"
      and it["revive"]["ts"] == "950.0")
check("revive: reply says resuming", "再開しました" in replies[-1])
check("revive: only expired items", g["_maybe_revive"](m, MGMT, "900.0") is None)  # もうpending
runtime.save_json("pending_approvals.json", pend_item())
check("revive: needs positive words",
      g["_maybe_revive"]({"ts": "951.0", "text": "<@U0BCCMPKD54> これなんだっけ？",
                          "user_id": runtime.TODA}, MGMT, "900.0") is None)

# ── apply: revive印をGO扱いで通常経路実行（バインディング・成立確認込み） ──
A = f"{REPO}/profile/skills/lipple/apply-ruling/scripts/run.py"
ga = {"__file__": A, "__name__": "apply_mod"}
exec(compile(open(A).read(), A, "exec"), ga)
posted = []
source.post_thread_reply = lambda ch, ts, text: posted.append((ch, ts, text)) or {"ok": True, "ts": "99.9"}
def fake_read(ch, root):
    if ch == MGMT:
        return [{"ts": "900.0", "user_id": runtime.CHIAKI_SELF, "text": "提案"},
                {"ts": "950.0", "user_id": runtime.TODA, "text": "<@U0BCCMPKD54>\nこれも指摘で！"}]
    return [{"ts": root, "user_id": "U09T44VEZM1", "text": "対応期限が2026年07月128日です"}]
source.read_thread = fake_read
pend = runtime.load_json("pending_approvals.json", {})
it = pend["items"]["900.0"]
it["status"], it["revive"] = "pending", {"ts": "950.0", "text": "これも指摘で！", "at": now}
r = ga["_rule_one"](pend, "900.0", it)
check("apply executes revive as GO", r == 1 and it["status"] == "awaiting_completion"
      and len(posted) == 2 and posted[0][0] == "CSRC")
check("binding bound to revive message",
      it["approval"]["verdict"] == "go"
      and ledger.entry(f"{MGMT}:950.0").get("status") == "ruled")
check("revive mark consumed", "revive" not in it)

# 冪等: 同じrevive発話は再実行されない（ruled済み）
posted.clear()
it2 = pend["items"]["900.0"]
it2["status"], it2["revive"] = "pending", {"ts": "950.0", "text": "これも指摘で！", "at": now}
r = ga["_rule_one"](pend, "900.0", it2)
check("revive idempotent on ruled ts", r == 0 and not posted)

# 対象が既に直っていたら already_fixed で正直にクローズ
def fake_read_fixed(ch, root):
    if ch == MGMT:
        return fake_read(MGMT, root)
    return [{"ts": root, "user_id": "U09T44VEZM1", "text": "対応期限が2026年07月28日です"}]
source.read_thread = fake_read_fixed
runtime.save_json("pending_approvals.json", pend_item())
pend = runtime.load_json("pending_approvals.json", {})
it3 = pend["items"]["900.0"]
it3["status"], it3["revive"] = "pending", {"ts": "951.0", "text": "これも指摘で！", "at": now}
posted.clear()
r = ga["_rule_one"](pend, "900.0", it3)
check("revive on already-fixed target closes honestly",
      r == 1 and it3["status"] == "already_fixed"
      and any("すでに直っていた" in p[2] for p in posted))

# ── listener: 相手スレッド(_is_relevant)でもChiaki宛メンションの非裁定発話はintakeへ ──
src = open(f"{REPO}/profile/skills/lipple/event-listener/scripts/run.py").read()
check("listener routes mention conversation to intake in relevant threads",
      "Chiaki宛メンション付きの非裁定発話は会話（intake）へ" in src)

print(f"\n{ok} checks passed")
