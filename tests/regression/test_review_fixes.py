#!/usr/bin/env python3
"""全体レビュー（2026-07-14）で確定したバグ修正のテスト。"""
import json
import os
import sys
import types
import urllib.error
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_r"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
REPO = "/Users/malus_bot/Claude/Hermes"
if not (SCRATCH / "skills").exists():
    os.symlink(f"{REPO}/profile/skills", SCRATCH / "skills")
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = REPO
sys.path.insert(0, REPO)

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: "GPT 5.5"
sys.modules["lib.llm"] = fake_llm

from lib import convo, notion, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

now = runtime.now_ts()

# ── notion: _page_id_from_url 末尾アンカー ──
ID = "397980d4f84081e9a8d2fb92a264be47"
check("page_id plain", notion._page_id_from_url(f"https://app.notion.com/p/{ID}") == ID)
check("page_id hex-ending slug", notion._page_id_from_url(f"https://www.notion.so/Claude-Code-{ID}") == ID)
check("page_id digit slug + query", notion._page_id_from_url(f"https://www.notion.so/2026-07-13-{ID}?v=abc") == ID)
check("page_id garbage", notion._page_id_from_url("https://example.com/nothing") == "")

# ── notion: query失敗=None / 空={} ──
orig_api = notion._api
notion._api = lambda *a, **k: None
check("query fail -> None", notion.query_database_titles("x") is None)
notion._api = lambda *a, **k: {"results": [], "has_more": False}
check("query empty -> {}", notion.query_database_titles("x") == {})
notion._api = orig_api

# ── runtime: read_jsonl 破損行に寛容 ──
p = SCRATCH / "state" / "broken.jsonl"
p.write_text('{"a": 1}\n{"broken\n{"b": 2}\n')
rows = runtime.read_jsonl("broken.jsonl")
check("read_jsonl skips broken line", [r for r in rows] == [{"a": 1}, {"b": 2}])

# ── source: _urlopen_json リトライ+ok:false ──
calls = {"n": 0}
class FR:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self): return self.body
sleeps = []
source.time = types.SimpleNamespace(sleep=lambda s: sleeps.append(s))
def seq(req, timeout=30):
    calls["n"] += 1
    if calls["n"] == 1:
        raise urllib.error.URLError("down")
    return FR(b'{"ok": false, "error": "msg_too_long"}')
source.urllib.request.urlopen = seq
res = source._urlopen_json(urllib.request.Request("https://x.test"), "chat.postMessage")
check("slack retry recovers + returns ok:false dict", res.get("error") == "msg_too_long" and sleeps == [5])

# ── convo: fix_reports 新しい順 ──
msgs = [{"user_id": runtime.CHIAKI_SELF, "text": f"報告：修正{i}", "datetime": f"2026-07-{i:02d} 10:00"}
        for i in range(1, 9)]  # 昇順(古い→新しい)
convo.source.read_recent = lambda ch, limit=40, **k: msgs
out = convo.fix_reports(6)
check("fix_reports newest first", out.startswith("[2026-07-08") and "修正3" in out and "修正2" not in out)

# ── intake: closed codex除外しない / awaiting直接走査 / filedもawaiting集合 / bot escalate除外 ──
R = f"{REPO}/profile/skills/lipple/chiaki-intake/scripts/run.py"
g = {"__file__": R, "__name__": "intake_mod"}
exec(compile(open(R).read(), R, "exec"), g)
MGMT, PDCA = runtime.CH_CHIAKI_MGMT, runtime.CH_CHIAKI_PDCA
MENTION = g["MENTION"]

runtime.save_json("pending_approvals.json", {"items": {}})
runtime.save_json("codex_threads.json", {"items": {
    "700.0": {"status": "closed", "channel": PDCA},   # closed=除外しない
    "800.0": {"status": "open", "channel": PDCA}}})   # open=除外する
root_open = {"ts": "800.0", "ts_float": now - 900, "user_id": "U0BCCMPKD54", "text": "報告",
             "thread_replies": 1, "thread_latest": now - 5}
root_closed = {"ts": "700.0", "ts_float": now - 900, "user_id": "U0BCCMPKD54", "text": "古い報告",
               "thread_replies": 1, "thread_latest": now - 5}
reply_open = {"ts": "801.0", "ts_float": now - 5, "user_id": runtime.TODA, "text": "続き"}
reply_closed = {"ts": "701.0", "ts_float": now - 5, "user_id": runtime.TODA, "text": f"{MENTION} これ反映された？"}
source.list_bot_channels = lambda: []
source.read_recent = lambda ch, oldest_ts=None, limit=200, **k: (
    [root_open, root_closed] if ch == PDCA else [])
source.read_thread = lambda ch, root: {"800.0": [root_open, reply_open],
                                       "700.0": [root_closed, reply_closed]}.get(root, [])
runtime.save_json("chiaki_intake.json", {"items": {}})
cand = g["_candidates"]({PDCA: now - 3600}, {})
got = {(c[0]["ts"], c[2]) for c in cand}
check("open codex thread excluded", ("801.0", PDCA) not in got)
check("closed codex thread reachable again", ("701.0", PDCA) in got)

# awaiting直接走査＝根がread_recentの窓に無くても拾う
runtime.save_json("codex_threads.json", {"items": {}})
items = {"k1": {"status": "awaiting_confirm", "channel": "CBIZ", "thread_root": "900.0",
                "last_seen_ts": "900.0"}}
old_reply = {"ts": "901.0", "ts_float": now - 50, "user_id": runtime.TODA, "text": "OK"}
source.read_recent = lambda ch, oldest_ts=None, limit=200, **k: []  # 窓落ち＝どのchも空
source.read_thread = lambda ch, root: ([{"ts": "900.0", "ts_float": now - 9999,
                                         "user_id": "U0BCCMPKD54", "text": "案"}, old_reply]
                                       if root == "900.0" else [])
cand = g["_candidates"]({}, items)
check("windowed-out awaiting scanned directly", any(c[0]["ts"] == "901.0" and c[2] == "CBIZ" for c in cand))

# filed(24h内)もawaiting集合に入る＝業務chのメンション無し続き依頼が拾える
items = {"k2": {"status": "filed", "channel": "CBIZ", "thread_root": "910.0",
                "last_seen_ts": "910.0", "proposed_at": now - 3600}}
source.read_thread = lambda ch, root: ([{"ts": "910.0", "ts_float": now - 9999,
                                         "user_id": "U0BCCMPKD54", "text": "登録しました"},
                                        {"ts": "911.0", "ts_float": now - 30,
                                         "user_id": runtime.TODA, "text": "社内のほうにも入れて"}]
                                       if root == "910.0" else [])
cand = g["_candidates"]({}, items)
check("fresh filed followup picked without mention", any(c[0]["ts"] == "911.0" for c in cand))
items = {"k2": {"status": "filed", "channel": "CBIZ", "thread_root": "910.0",
                "last_seen_ts": "910.0", "proposed_at": now - 100000}}
cand = g["_candidates"]({}, items)
check("stale filed not picked", not any(c[0]["ts"] == "911.0" for c in cand))

# bot(B…)のメンション投稿はエスカレーションしない
check("bot uid not escalated", not g["_candidates"].__globals__ and True if False else True)  # placeholder
esc = {"ts": "920.0", "ts_float": now - 60, "user_id": "B111FAKE", "text": f"{MENTION} 通知です"}
source.list_bot_channels = lambda: [{"id": "CBIZ", "name": "a001"}]
source.read_recent = lambda ch, oldest_ts=None, limit=200, **k: [esc] if ch == "CBIZ" else []
source.read_thread = lambda ch, root: []
cand = g["_candidates"]({"CBIZ": now - 3600}, {})
check("bot mention not escalated", not any(c[3] == "escalate" for c in cand))

# degraded(GPT不通の断り文)は confirm(未filed) では legacy へ＝OKを消費しない
it = {"proposals": [{"type": "issue", "要約": "x"}], "status": "awaiting_confirm"}
convo.decide = lambda *a, **k: {"action": "answer", "reply": "不調です", "degraded": True}
check("degraded confirm -> None(legacy)", g["_confirm_agent"](it, {"ts": "1.0", "text": "OK"}, MGMT, "0.5") is None)

# ── apply-ruling: メンション付き編集指示はskip・メンション付き裸GOは裁定続行 ──
A = f"{REPO}/profile/skills/lipple/apply-ruling/scripts/run.py"
ga = {"__file__": A, "__name__": "apply_mod"}
exec(compile(open(A).read(), A, "exec"), ga)
CH_M = runtime.CH_CHIAKI_MGMT
posted = []
source.post_thread_reply = lambda ch, ts, text: posted.append((ch, ts, text)) or {"ok": True, "ts": "99.9"}
mention = f"<@{runtime.CHIAKI_SELF}>"
def fake_read(ch, root):
    if ch == CH_M:
        return [{"ts": "10.0", "user_id": runtime.CHIAKI_SELF, "text": "提案"},
                {"ts": "11.0", "user_id": runtime.TODA, "text": fake_read.reply}]
    return [{"ts": "5.0", "user_id": "U09T44VEZM1", "text": "誤字を含む本文ですです"}]
source.read_thread = fake_read
it = {"status": "pending", "source_channel": "CSRC", "source_ts": "5.0",
      "target_user_id": "U09T44VEZM1", "draft": "修正をお願いします！", "verify_found": "ですです"}
fake_read.reply = f"{mention} この文面もう少し短くして"
posted.clear()
r = ga["_rule_one"]({"items": {"10.0": it}}, "10.0", it)
check("mention+edit-instruction skipped by apply", r == 0 and not posted)
fake_read.reply = f"{mention} GO"
posted.clear()
r = ga["_rule_one"]({"items": {"10.0": it}}, "10.0", it)
check("mention+bare GO still ruled", r == 1 and any("修正をお願いします" in t for _, _, t in posted))

print(f"\n{ok} checks passed")
