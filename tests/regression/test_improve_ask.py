#!/usr/bin/env python3
"""改善話題→「Issueとして処理しますか？」確認（2026-07-21 戸田・表記は英字Issue）のテスト。"""
import json, os, sys, types
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path(__file__).parent / "state_ia"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = str(ROOT)
sys.path.insert(0, str(ROOT))

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: "GPT 5.5"
sys.modules["lib.llm"] = fake_llm

from lib import convo, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond: sys.exit(1)
    ok += 1

# 1. 会話コア: 全モードでproposeが許可され、聞き方の指定がプロンプトに載る
check("codex_thread allows propose", "propose" in convo.MODES["codex_thread"])
check("ask phrase in guidance", "Issueとして処理しますか" in convo.ACTIONS["propose"])
check("katakana banned in guidance", "「イシュー」と書かない" in convo.ACTIONS["propose"])
check("improvement topics covered", "改善" in convo.ACTIONS["propose"])

# 2. codex-runner: propose → intake確認ターンへ引き継ぎ
R = str(ROOT / "profile/skills/lipple/codex-runner/scripts/run.py")
g = {"__file__": R, "__name__": "codex_mod"}
exec(compile(open(R).read(), R, "exec"), g)
now = runtime.now_ts()
runtime.save_json("codex_threads.json", {"items": {
    "500.0": {"status": "open", "channel": "C0BC6PPG013", "branch": "codex/qX",
              "summary": "作業", "last_seen_ts": now - 3600}}})
runtime.save_json("chiaki_intake.json", {"items": {}})
posted = []
source.post_thread_reply = lambda ch, ts, text: posted.append((ch, ts, text)) or {"ok": True, "ts": "9.9"}
source.read_thread = lambda ch, root: [
    {"ts": "500.0", "ts_float": now - 7200, "user_id": runtime.CHIAKI_SELF, "text": "報告"},
    {"ts": "501.0", "ts_float": now - 60, "user_id": runtime.TODA,
     "text": "ついでにこの辺のテストも整備したいね"}]
convo.decide = lambda *a, **k: {"action": "propose",
                                "reply": "テスト整備、Issueとして処理しますか？",
                                "proposals": [{"type": "issue", "issue_kind": "変更",
                                               "要約": "テストの整備", "詳細": "x"}]}
convo.already_replied = lambda ch, ts: False
convo.commit = lambda: None
g["_process_threads"]()
items = runtime.load_json("chiaki_intake.json", {"items": {}})["items"]
check("handoff to intake awaiting", "501.0" in items and items["501.0"]["status"] == "awaiting_confirm")
check("proposal carried", items["501.0"]["proposals"][0]["要約"] == "テストの整備")
check("permalink built", "archives/C0BC6PPG013/p501" in items["501.0"]["permalink"])
check("ask posted", any("Issueとして処理しますか" in t for _, _, t in posted))
reg = runtime.load_json("codex_threads.json", {})["items"]["500.0"]
check("last_seen advanced", float(reg["last_seen_ts"]) >= now - 60)

# 3. 案が壊れている場合は返事のみ（intakeに空の確認を作らない）
runtime.save_json("chiaki_intake.json", {"items": {}})
runtime.save_json("codex_threads.json", {"items": {
    "600.0": {"status": "open", "channel": "C0BC6PPG013", "last_seen_ts": now - 3600}}})
source.read_thread = lambda ch, root: [
    {"ts": "600.0", "ts_float": now - 7200, "user_id": runtime.CHIAKI_SELF, "text": "報告"},
    {"ts": "601.0", "ts_float": now - 60, "user_id": runtime.TODA, "text": "x"}]
convo.decide = lambda *a, **k: {"action": "propose", "reply": "どうしますか？", "proposals": []}
posted.clear()
g["_process_threads"]()
check("no empty awaiting", not runtime.load_json("chiaki_intake.json", {"items": {}})["items"])
check("reply still sent", len(posted) == 1)

print(f"\n{ok} checks passed")
