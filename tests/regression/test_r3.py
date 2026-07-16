#!/usr/bin/env python3
"""再設計R3（承認バインディング＋単一ロック）のテスト。"""
import hashlib
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_r3"
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

from lib import ledger, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

now = runtime.now_ts()

# ── runtime.approvals_lock: 取得・解放・再取得 ──
with runtime.approvals_lock():
    pass
with runtime.approvals_lock():
    x = 1
check("approvals_lock reentrant sequential", x == 1)

# ── apply-ruling: 冪等（台帳ruled済みのGOは再実行しない）＋バインディング ──
A = f"{REPO}/profile/skills/lipple/apply-ruling/scripts/run.py"
ga = {"__file__": A, "__name__": "apply_mod"}
exec(compile(open(A).read(), A, "exec"), ga)
MGMT = runtime.CH_CHIAKI_MGMT
posted = []
source.post_thread_reply = lambda ch, ts, text: posted.append((ch, ts, text)) or {"ok": True, "ts": "99.9"}
def fake_read(ch, root):
    if ch == MGMT:
        return [{"ts": "10.0", "user_id": runtime.CHIAKI_SELF, "text": "提案"},
                {"ts": "11.0", "user_id": runtime.TODA, "text": "GO"}]
    return [{"ts": "5.0", "user_id": "U09T44VEZM1", "text": "誤字を含む本文ですです"}]
source.read_thread = fake_read

def fresh_item():
    return {"status": "pending", "source_channel": "CSRC", "source_ts": "5.0",
            "target_user_id": "U09T44VEZM1", "draft": "修正をお願いします！",
            "verify_found": "ですです"}

# 1回目のGO: 実行される＋binding記録
it = fresh_item()
r = ga["_rule_one"]({"items": {"10.0": it}}, "10.0", it)
check("GO executes", r == 1 and len(posted) == 2)  # 対象スレッド+事後報告
ap = it.get("approval") or {}
exp_digest = hashlib.sha1("修正をお願いします！".encode()).hexdigest()[:12]
check("binding on item", ap.get("proposal") == "10.0" and ap.get("digest") == exp_digest
      and ap.get("approver") == runtime.TODA and ap.get("verdict") == "go")
led = ledger.entry(f"{MGMT}:11.0")
check("binding in ledger", led.get("status") == "ruled"
      and (led.get("refs") or {}).get("approval", {}).get("digest") == exp_digest)

# 同じGO発話の再処理: 台帳冪等で何もしない（状態ファイルが巻き戻っても）
posted.clear()
it2 = fresh_item()  # 状態巻き戻りを再現＝pendingに戻った同じ提案
r = ga["_rule_one"]({"items": {"10.0": it2}}, "10.0", it2)
check("same GO not re-executed (ledger idempotent)", r == 0 and not posted)

# 却下パスもbinding
def fake_read2(ch, root):
    if ch == MGMT:
        return [{"ts": "20.0", "user_id": runtime.CHIAKI_SELF, "text": "提案"},
                {"ts": "21.0", "user_id": runtime.TODA, "text": "却下"}]
    return [{"ts": "6.0", "user_id": "U09T44VEZM1", "text": "本文"}]
source.read_thread = fake_read2
it3 = fresh_item()
it3["source_ts"] = "6.0"
r = ga["_rule_one"]({"items": {"20.0": it3}}, "20.0", it3)
check("reject binding", r == 1 and (it3.get("approval") or {}).get("verdict") == "reject")

# ── propose-to-approval: ロック下マージ＝並行遷移を消さない ──
P = f"{REPO}/profile/skills/lipple/propose-to-approval/scripts/run.py"
gp = {"__file__": P, "__name__": "propose_mod"}
exec(compile(open(P).read(), P, "exec"), gp)
# 事前: 台帳に既存アイテム（retract済み）
runtime.save_json("pending_approvals.json", {"items": {"800.0": {"status": "retracted"}}})
# findings 1件（new）
findings_row = {"ts": now, "kind": "typo", "channel": "CSRC", "msg_ts": "7.0",
                "task": "ですです", "issue": {"found": "ですです", "suggest": "です"}, "status": "new"}
(SCRATCH / "state" / "findings.jsonl").write_text(json.dumps(findings_row, ensure_ascii=False) + "\n")
gp["_context_precheck"] = lambda *a, **k: None
gp["_target"] = lambda ch: ("U09T44VEZM1", "松永")
gp["_rules"] = lambda: {}
gp["_permalink"] = lambda ch, ts: "http://x"
source.post_message = lambda ch, text: {"ok": True, "ts": "900.0"}
gp["main"]()
pend = runtime.load_json("pending_approvals.json", {})
check("propose merged under lock", pend["items"].get("800.0", {}).get("status") == "retracted"
      and "900.0" in pend["items"])

print(f"\n{ok} checks passed")
