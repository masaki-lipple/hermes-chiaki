#!/usr/bin/env python3
"""API不通の可視化と隔離（2026-08-06 実障害: Anthropicクレジット残高切れで8/3からHaiku全滅・
typo-scanは走査ごとクラッシュ・原因が「Bad Request」としか見えず3日盲目だった）のテスト。
①llm._call=エラー本文を例外へ ②typo-scan=検知失敗の隔離(カーソル据え置き)
③self-health._llm_dead=全滅警告 ④sync_notation._query=一時障害リトライ ⑤日次サマリの失敗表示。"""
import io
import json
import os
import sys
import urllib.error
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_ao"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
REPO = "/Users/malus_bot/Claude/Hermes"
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = REPO
os.environ["ANTHROPIC_API_KEY"] = "test-key"
sys.path.insert(0, REPO)

from lib import llm, runtime, source  # noqa: E402  # 本物のllm

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

now = runtime.now_ts()

# ── ① HTTPエラーの本文が例外に含まれる（「Bad Request」だけの盲目をなくす） ──
def http400(*a, **k):
    raise urllib.error.HTTPError("https://api.anthropic.com/v1/messages", 400, "Bad Request", {},
                                 io.BytesIO(b'{"error":{"message":"credit balance is too low"}}'))
llm.urllib.request.urlopen = http400
try:
    llm._call("claude-haiku-4-5", "hi", "sys", 10)
    raised = ""
except RuntimeError as e:
    raised = str(e)
check("① error body surfaced", "400" in raised and "credit balance" in raised)
try:
    llm.haiku("こんにちは")
except RuntimeError:
    pass
rows = runtime.read_jsonl("llm_usage.jsonl")
check("① usage note carries reason", rows and "credit balance" in rows[-1].get("note", ""))

# ── ② typo-scan: 検知失敗はチャンネル単位で隔離・カーソル据え置き・走査は完走 ──
T = f"{REPO}/profile/skills/lipple/typo-scan/scripts/run.py"
gt = {"__file__": T, "__name__": "typo_mod"}
exec(compile(open(T).read(), T, "exec"), gt)
source.list_bot_channels = lambda: [{"id": "CX", "name": "x"}]
gt["_gather"] = lambda ch, since, bots: ([{"ts": "1.0", "ts_float": now, "datetime": "d",
                                           "text": "ですですの件", "user_id": "U1"}], now)
def detect_down(msgs, known):
    raise RuntimeError("anthropic HTTP 400: credit balance is too low")
gt["_detect"] = detect_down
gt["main"]()  # 例外で落ちないこと
cur = runtime.load_json("typo_cursor.json", {})
check("② detect failure isolated + cursor kept", "CX" not in cur)
gt["_detect"] = lambda msgs, known: []
gt["main"]()
cur = runtime.load_json("typo_cursor.json", {})
check("② recovery advances cursor", float(cur.get("CX") or 0) == now)

# ── ③ self-health: 前営業日からの全滅を警告 ──
H = f"{REPO}/profile/skills/lipple/self-health/scripts/run.py"
gh = {"__file__": H, "__name__": "health_mod"}
exec(compile(open(H).read(), H, "exec"), gh)
(SCRATCH / "state" / "llm_usage.jsonl").unlink()
for i in range(4):
    runtime.append_jsonl("llm_usage.jsonl", {"ts": now - i * 60, "caller": "x", "fn": "haiku",
                                             "model": "Haiku 4.5", "ok": False, "ms": 1, "in": 1,
                                             "out": 0, "note": "RuntimeError: anthropic HTTP 400: credit balance is too low"})
warns = gh["_llm_dead"](now)
check("③ all-fail warned with reason", len(warns) == 1 and "credit balance" in warns[0])
runtime.append_jsonl("llm_usage.jsonl", {"ts": now, "caller": "x", "fn": "haiku",
                                         "model": "Haiku 4.5", "ok": True, "ms": 1, "in": 1, "out": 1})
check("③ mixed -> no warn", gh["_llm_dead"](now) == [])

# ── ④ sync_notation._query: 一時障害はリトライで生き残る ──
from lib import sync_notation  # noqa: E402
sync_notation.time.sleep = lambda s: None
calls = {"n": 0}
def flaky(req, timeout=30):
    calls["n"] += 1
    if calls["n"] == 1:
        raise ConnectionResetError(104, "Connection reset by peer")
    return io.StringIO(json.dumps({"results": [{"id": "r1"}], "has_more": False}))
sync_notation.urllib.request.urlopen = flaky
rows = sync_notation._query("db123", "tok")
check("④ query survives transient reset", calls["n"] == 2 and rows == [{"id": "r1"}])

# ── ⑤ 日次サマリのLLM行に失敗数 ──
D = f"{REPO}/profile/skills/lipple/daily-summary/scripts/run.py"
gd = {"__file__": D, "__name__": "ds_mod"}
exec(compile(open(D).read(), D, "exec"), gd)
text = gd["build"](now)
check("⑤ failures shown in daily summary", "失敗4回" in text)

print(f"\n{ok} checks passed")
