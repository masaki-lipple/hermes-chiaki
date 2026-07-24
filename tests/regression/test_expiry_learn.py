#!/usr/bin/env python3
"""2026-07-23 戸田決定の2件のテスト。
①pending提案の2日自動失効（「1は2日で自動失効」＝投稿せず閉じる・1回通知）
②理由付き却下の裁定化＋学習（「2は次回指摘しないようにする仕組みをつくる」＝
  「却下。これは本当の人物名。」をapplyが却下として処理し、reject_learn.jsonlへ学習。
  propose-to-approval/typo-scanが同じ検知語を再提案・再検知しない）。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_el"
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

# ════ ② 理由付き却下の分類・裁定・学習 ════
A = f"{REPO}/profile/skills/lipple/apply-ruling/scripts/run.py"
ga = {"__file__": A, "__name__": "apply_mod"}
exec(compile(open(A).read(), A, "exec"), ga)

v, p = ga["_classify"]("却下。これは本当の人物名。")
check("② reasoned reject classified", v == "reject" and "本当の人物名" in p)
check("② bare reject still works", ga["_classify"]("却下")[0] == "reject")
check("② reject-lead with edit verb -> interpret",
      ga["_classify"]("却下。かわりに一文足して")[0] == "interpret")
check("② ordinary text not reject", ga["_classify"]("この件どう思う")[0] != "reject")

posted = []
source.post_thread_reply = lambda ch, ts, text: posted.append((ch, ts, text)) or {"ok": True, "ts": "99.9"}
def fake_read(ch, root):
    if ch == MGMT:
        return [{"ts": "900.0", "user_id": runtime.CHIAKI_SELF, "text": "提案"},
                {"ts": "901.0", "user_id": runtime.TODA,
                 "text": "<@U0BCCMPKD54>\n却下。これは本当の人物名。"}]
    return [{"ts": root, "user_id": "U09T44VEZM1", "text": "白岩様の件ですです"}]
source.read_thread = fake_read
it = {"status": "pending", "source_channel": "CSRC", "source_ts": "5.0", "finding_kind": "typo",
      "target_user_id": "U09T44VEZM1", "draft": "修正をお願いします！", "verify_found": "白岩様"}
r = ga["_rule_one"]({"items": {"900.0": it}}, "900.0", it)
learn = list(runtime.read_jsonl("reject_learn.jsonl"))
check("② reasoned reject ruled (mention ok)", r == 1 and it["status"] == "reject")
check("② learned row written", len(learn) == 1 and learn[0]["found"] == "白岩様"
      and "本当の人物名" in learn[0]["reason"])
check("② report mentions learning", any("学習" in p2[2] for p2 in posted))
check("② ruled in ledger", ledger.entry(f"{MGMT}:901.0").get("status") == "ruled")

# 裸の「却下」（理由なし）は学習しない＝「今回は不要」の可能性があるため抑止に使わない
def fake_read_bare(ch, root):
    if ch == MGMT:
        return [{"ts": "905.0", "user_id": runtime.CHIAKI_SELF, "text": "提案"},
                {"ts": "906.0", "user_id": runtime.TODA, "text": "却下"}]
    return [{"ts": root, "user_id": "U09T44VEZM1", "text": "本文ですです"}]
source.read_thread = fake_read_bare
it_b = {"status": "pending", "source_channel": "CSRC", "source_ts": "6.0", "finding_kind": "typo",
        "target_user_id": "U09T44VEZM1", "draft": "x", "verify_found": "ですです"}
r = ga["_rule_one"]({"items": {"905.0": it_b}}, "905.0", it_b)
check("② bare reject rules but does not learn",
      r == 1 and it_b["status"] == "reject" and len(list(runtime.read_jsonl("reject_learn.jsonl"))) == 1)

# ════ ② intakeの経路: 理由付き却下はapply領分 ════
R = f"{REPO}/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)
MENTION = g["MENTION"]
check("② _is_ruling_message: bare GO", g["_is_ruling_message"](f"{MENTION} GO"))
check("② _is_ruling_message: reasoned reject",
      g["_is_ruling_message"](f"{MENTION} 却下。これは本当の人物名。"))
check("② _is_ruling_message: ordinary false", not g["_is_ruling_message"](f"{MENTION} これ直せる？"))

runtime.save_json("pending_approvals.json", {"items": {"910.0": {"status": "pending"}}})
runtime.append_jsonl("exec_ledger.jsonl", {"id": f"{MGMT}:911.0", "at": now - 60, "owner": "intake",
                                           "status": "received", "ch": MGMT, "ts": "911.0",
                                           "thread_root": "910.0"})
g_threads = {"910.0": [{"ts": "910.0", "ts_float": now - 300, "user_id": runtime.CHIAKI_SELF, "text": "提案"},
                       {"ts": "911.0", "ts_float": now - 60, "user_id": runtime.TODA,
                        "text": f"{MENTION} 却下。理由はこれこれ。"}]}
source.read_thread = lambda ch, root: g_threads.get(root, [])
cand = g["_ledger_candidates"]({})
check("② intake skips reasoned reject as apply領分",
      not any(c[0]["ts"] == "911.0" for c in cand)
      and ledger.entry(f"{MGMT}:911.0").get("note") == "apply領分")

# ════ ① 2日自動失効 ════
old_ts, fresh_ts = str(now - 3 * 86400), str(now - 3600)
pend = {"items": {
    old_ts: {"status": "pending", "finding_kind": "typo", "draft": "古い提案"},
    fresh_ts: {"status": "pending", "finding_kind": "typo", "draft": "新しい提案"},
    "1.0": {"status": "completed"}}}
posted.clear()
n = ga["_phase_expiry"](pend)
check("① old pending expired", n == 1 and pend["items"][old_ts]["status"] == "expired")
check("① fresh pending kept", pend["items"][fresh_ts]["status"] == "pending")
check("① notice posted once with no-post note",
      len(posted) == 1 and "自動で失効" in posted[0][2] and "投稿はしていません" in posted[0][2])
rul = [r2 for r2 in runtime.read_jsonl("rulings.jsonl") if r2.get("verdict") == "expired"]
check("① expiry recorded in rulings", len(rul) == 1 and rul[0]["thread_ts"] == old_ts)
posted.clear()
n = ga["_phase_expiry"](pend)
check("① idempotent (no re-notice)", n == 0 and not posted)

# expiredのスレッド行はsweepで終端される
runtime.append_jsonl("exec_ledger.jsonl", {"id": "CSRC:70.1", "at": now - 100, "owner": "apply",
                                           "status": "received", "ch": "CSRC", "ts": "70.1",
                                           "thread_root": "70.0"})
pend2 = {"items": {"960.0": {"status": "expired", "source_channel": "CSRC", "source_ts": "70.0"}}}
ga["_ledger_sweep"](pend2)
check("① expired item rows swept", ledger.entry("CSRC:70.1").get("status") == "skipped")

# ════ ② 抑止: propose-to-approval ════
P = f"{REPO}/profile/skills/lipple/propose-to-approval/scripts/run.py"
gp = {"__file__": P, "__name__": "propose_mod"}
exec(compile(open(P).read(), P, "exec"), gp)
runtime.save_json("pending_approvals.json", {"items": {}})
(SCRATCH / "state" / "findings.jsonl").write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in [
    {"ts": now, "kind": "typo", "channel": "CSRC", "msg_ts": "7.0", "task": "x",
     "issue": {"found": "白岩様", "suggest": "白石様"}, "status": "new"},
    {"ts": now, "kind": "typo", "channel": "CSRC", "msg_ts": "8.0", "task": "y",
     "issue": {"found": "ですです", "suggest": "です"}, "status": "new"}]) + "\n")
gp["_context_precheck"] = lambda *a, **k: None
gp["_target"] = lambda ch: ("U09T44VEZM1", "松永")
gp["_rules"] = lambda: {}
gp["_permalink"] = lambda ch, ts: "http://x"
runtime.is_jp_workday = lambda ts=None: True
sent = []
source.post_message = lambda ch, text: sent.append(text) or {"ok": True, "ts": "990.0"}
gp["main"]()
finds = runtime.read_jsonl("findings.jsonl")
st = {f["issue"]["found"]: f["status"] for f in finds}
check("② learned found suppressed", st["白岩様"] == "rejected_learned")
check("② other finding still proposed", st["ですです"] == "proposed" and len(sent) == 1
      and "白岩" not in sent[0])

# ════ ② 抑止: typo-scan ════
T = f"{REPO}/profile/skills/lipple/typo-scan/scripts/run.py"
gt = {"__file__": T, "__name__": "typo_mod"}
exec(compile(open(T).read(), T, "exec"), gt)
known = gt["_known"]({"terms": [{"official": "Lipple"}], "acronyms": ["PDCA"]}, {"白岩様"})
check("② learned in known terms", "白岩様" in known and "Lipple" in known)
source.list_bot_channels = lambda: [{"id": "CX", "name": "x"}]
gt["_gather"] = lambda ch, since, bots: ([{"ts": "1.0", "ts_float": now, "datetime": "d",
                                           "text": "白岩様とですですの以の件", "user_id": "U1"}], now)
gt["_detect"] = lambda msgs, known: [{"i": 0, "found": "白岩様", "suggest": "白石様"},
                                     {"i": 0, "found": "ですです", "suggest": "です"},
                                     {"i": 0, "found": "以", "suggest": "以下"}]
before = len(runtime.read_jsonl("findings.jsonl"))
gt["main"]()
new_finds = runtime.read_jsonl("findings.jsonl")[before:]
founds = [f["issue"]["found"] for f in new_finds]
check("② typo-scan skips learned found", "白岩様" not in founds and "ですです" in founds)
check("② typo-scan skips 1-char found", "以" not in founds)  # 2026-07-24「以」誤検知の再発防止

print(f"\n{ok} checks passed")
