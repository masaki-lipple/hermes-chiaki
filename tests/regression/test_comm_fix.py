#!/usr/bin/env python3
"""意思疎通の故障修正（2026-07-13 戸田「意思疎通がうまくとれないことが問題」）のテスト。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_m"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = "/Users/malus_bot/Claude/Hermes"
sys.path.insert(0, "/Users/malus_bot/Claude/Hermes")

fake_llm = types.ModuleType("lib.llm")
fake_llm._tag = ""
fake_llm._script = []   # [(tag, output)] を順に返す。尽きたら最後を繰り返す
fake_llm._i = 0
fake_llm.last_prompt = ""
def _gpt(p, system=None, max_tokens=450, timeout=90):
    fake_llm.last_prompt = p
    i = min(fake_llm._i, len(fake_llm._script) - 1)
    fake_llm._i += 1
    tag, out = fake_llm._script[i]
    fake_llm._tag = tag
    return out
fake_llm.gpt = _gpt
fake_llm.reset_used = lambda: setattr(fake_llm, "_tag", "")
fake_llm.last_used = lambda: fake_llm._tag
sys.modules["lib.llm"] = fake_llm

from lib import convo, runtime, source  # noqa: E402

convo.fix_reports = lambda n=6: "（テスト）"
convo.source.read_thread = lambda ch, root: []
sleeps = []
convo.time = types.SimpleNamespace(sleep=lambda s: sleeps.append(s))

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

def script(*steps):
    fake_llm._script = list(steps)
    fake_llm._i = 0

ANS = json.dumps({"action": "answer", "reply": "はい、任せてください！"})

# ── ③ GPT不通リトライ＋正直文 ──
script(("Haiku 4.5・代替", "こわれ"), ("GPT 5.5", ANS))
sleeps.clear()
d = convo.decide("C1", "10.0", {"ts": "11.0", "text": "<@U0BCCMPKD54> これどう？"}, mode="initial")
check("retry recovers", d and d["reply"] == "はい、任せてください！")
check("retry slept once", sleeps == [20])

script(("Haiku 4.5・代替", "x"))
sleeps.clear()
d = convo.decide("C1", "10.0", {"ts": "12.0", "text": "<@U0BCCMPKD54> これどう？"}, mode="initial")
check("outage -> honest reply", d and d.get("degraded") and "不調" in d["reply"])
check("outage slept twice", sleeps == [20, 20])
check("outage reply untagged", fake_llm.last_used() == "")

sleeps.clear()
d = convo.decide("C1", "10.0", {"ts": "13.0", "text": "メンションなしのFYI"}, mode="initial")
check("outage + no mention -> None(legacy)", d is None)

# ── ⑤ already_replied ──
script(("GPT 5.5", ANS))
d = convo.decide("C1", "20.0", {"ts": "21.0", "text": "<@U0BCCMPKD54> テスト"}, mode="initial")
check("m_ts staged", convo._last and convo._last["m_ts"] == "21.0")
convo.commit()
check("already_replied hit", convo.already_replied("C1", "21.0"))
check("already_replied miss other ts", not convo.already_replied("C1", "22.0"))
check("already_replied miss other ch", not convo.already_replied("C2", "21.0"))
mem = convo.memory()
mem["ledger"].append({"ch": "C1", "m_ts": "30.0", "action": "silent", "reply": ""})
runtime.save_json(convo.MEM_FILE, mem)
check("silent not counted", not convo.already_replied("C1", "30.0"))

# ── ④ プロンプト規約＋②file指示 ──
script(("GPT 5.5", ANS))
convo.decide("C1", "40.0", {"ts": "41.0", "text": "<@U0BCCMPKD54> ？"}, mode="confirm")
p = fake_llm.last_prompt
check("no-internal-leak rule in prompt", "内部の取得・表示の都合" in p)
check("file action tells codex in reply", "あなたの一文で完結させる" in p)

# ── intake: #5902/業務chのCodexスレッド除外＋file連結の重ね掛け解消 ──
R = "/Users/malus_bot/Claude/Hermes/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)
MGMT, PDCA = runtime.CH_CHIAKI_MGMT, runtime.CH_CHIAKI_PDCA
MENTION = g["MENTION"]
now = runtime.now_ts()

runtime.save_json("codex_threads.json", {"items": {"555.0": {"status": "open", "channel": PDCA}}})
runtime.save_json("pending_approvals.json", {"items": {}})
runtime.save_json("chiaki_intake.json", {"items": {}})
root_msg = {"ts": "555.0", "ts_float": now - 900, "user_id": "U0BCCMPKD54",
            "text": "Codexの報告", "thread_replies": 2, "thread_latest": now - 10}
reply_msg = {"ts": "556.0", "ts_float": now - 10, "user_id": runtime.TODA,
             "text": "続きお願い", "datetime": "2026-07-13 17:00"}
source.list_bot_channels = lambda: [{"id": "CBIZ", "name": "a040"}]
def rr(ch, oldest_ts=None, limit=200, **k):
    return [root_msg] if ch in (PDCA, "CBIZ") else []
source.read_recent = rr
source.read_thread = lambda ch, root: [root_msg, reply_msg]
cand = g["_candidates"]({PDCA: now - 3600, "CBIZ": now - 3600}, {})
check("pdca codex thread excluded", not any(c[0]["ts"] == "556.0" and c[2] == PDCA for c in cand))
check("biz-ch codex thread excluded", not any(c[0]["ts"] == "556.0" and c[2] == "CBIZ" for c in cand))
runtime.save_json("codex_threads.json", {"items": {}})
cand = g["_candidates"]({PDCA: now - 3600, "CBIZ": now - 3600}, {})
check("non-codex thread still scanned", any(c[0]["ts"] == "556.0" and c[2] == PDCA for c in cand))

# file時の継ぎ足し解消
posted = []
g["_reply"] = lambda ch, root, text, *a, **k: posted.append(text)
g["_file_issue"] = lambda p, link, ch: "http://issue/1"
g["_maybe_enqueue_codex"] = lambda *a, **k: "\nCodexに実装させます！進捗はこのスレッドに報告します。"
g["_maybe_edit_root"] = lambda *a, **k: "skip"
def fake_decide(ch, root, m, mode, extra_facts=None):
    return fake_decide.out
convo.decide = fake_decide
it = {"proposals": [{"type": "issue", "issue_kind": "変更", "要約": "並び順", "詳細": "x"}],
      "status": "awaiting_confirm", "permalink": "http://p", "mention_text": "直して", "propose_count": 1}
fake_decide.out = {"action": "file", "codex": True,
                   "reply": "Issueに登録しました！この件はCodexに回して、進捗はこのスレッドに報告します。"}
g["_confirm_agent"](it, {"ts": "70.0", "user_id": runtime.TODA, "text": "お願いします！"}, MGMT, "60.0")
check("codex note deduped", len(posted) == 1 and "Codexに実装させます" not in posted[0])
check("url still attached", "http://issue/1" in posted[0])
it2 = {"proposals": [{"type": "issue", "issue_kind": "変更", "要約": "並び順", "詳細": "x"}],
       "status": "awaiting_confirm", "permalink": "http://p", "mention_text": "直して", "propose_count": 1}
fake_decide.out = {"action": "file", "codex": True, "reply": "Issueに登録しました！"}
posted.clear()
g["_confirm_agent"](it2, {"ts": "71.0", "user_id": runtime.TODA, "text": "OK"}, MGMT, "61.0")
check("codex note kept when reply lacks it", "Codexに実装させます" in posted[0])

print(f"\n{ok} checks passed")
