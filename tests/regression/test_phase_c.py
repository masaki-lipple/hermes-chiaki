#!/usr/bin/env python3
"""Phase C（スレッドを跨ぐ記憶）のテスト。"""
import json
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_c"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)  # 冪等性=前回の状態を残さない
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
sys.path.insert(0, "/Users/malus_bot/Claude/Hermes")

fake_llm = types.ModuleType("lib.llm")
fake_llm.last_prompt = ""
fake_llm.next_out = ""
fake_llm.gpt = lambda p, system=None, max_tokens=450, timeout=90: (
    setattr(fake_llm, "last_prompt", p) or fake_llm.next_out)
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: "GPT 5.5"
sys.modules["lib.llm"] = fake_llm

from lib import convo, runtime  # noqa: E402

convo.fix_reports = lambda n=6: "（テスト）"
convo.source.read_thread = lambda ch, root: [{"user_id": runtime.TODA, "text": "テスト発話"}]

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

# 1. decide 成功 → _last 仮置き → commit で台帳へ
fake_llm.next_out = json.dumps({"action": "answer", "reply": "はい、そうです！"})
d = convo.decide("C0BCE19BN2G", "111.0", {"text": "これどうなってる？"}, mode="initial")
check("decide returns action", d and d["action"] == "answer")
check("_last staged", convo._last and convo._last["root"] == "111.0" and convo._last["action"] == "answer")
convo.commit()
mem = convo.memory()
check("commit appends ledger", len(mem["ledger"]) == 1 and mem["ledger"][0]["said"] == "これどうなってる？")
check("commit clears _last", convo._last is None)
convo.commit()  # 二重 commit は無害
check("double commit no-op", len(convo.memory()["ledger"]) == 1)

# 2. decide 失敗（不正出力）→ _last リセット＝古い判断が誤記録されない
fake_llm.next_out = json.dumps({"action": "answer", "reply": "古い判断"})
convo.decide("C0BCE19BN2G", "222.0", {"text": "a"}, mode="initial")
check("stale _last staged", convo._last is not None)
fake_llm.next_out = "こわれた出力"
r = convo.decide("C0BCE19BN2G", "333.0", {"text": "b"}, mode="initial")
check("broken output -> None", r is None)
check("broken output resets _last", convo._last is None)
convo.commit()
check("no stale record", len(convo.memory()["ledger"]) == 1)

# 3. プロンプトに長期記憶＋別スレッドが注入される（今のスレッド分は除外）
mem = convo.memory()
mem["notes"] = [{"note": "表記は全角かっこを使う", "kind": "決定", "since": "2026-07-08"}]
runtime.save_json(convo.MEM_FILE, mem)
fake_llm.next_out = json.dumps({"action": "answer", "reply": "覚えています！"})
convo.decide("C0BCE19BN2G", "999.0", {"text": "さっきの件どうなった？"}, mode="initial")
p = fake_llm.last_prompt
check("notes injected", "[決定] 表記は全角かっこを使う" in p)
check("cross-thread injected", "これどうなってる？" in p and "はい、そうです！" in p)
fake_llm.next_out = json.dumps({"action": "answer", "reply": "x"})
convo.decide("C0BCE19BN2G", "111.0", {"text": "続きです"}, mode="initial")
check("same-root excluded from cross-thread",
      "これどうなってる？" not in fake_llm.last_prompt.split("# スレッドのやりとり")[0])

# 4. propose の gist が台帳に載る
fake_llm.next_out = json.dumps({"action": "propose", "reply": "起票しますか？",
                                "proposals": [{"type": "issue", "要約": "通知の宛先が違う"}]})
convo.decide("C0BCE19BN2G", "444.0", {"text": "宛先ちがうよ"}, mode="initial")
check("gist recorded", convo._last and convo._last.get("gist") == "通知の宛先が違う")
convo.commit()

# 5. リングバッファ上限
mem = convo.memory()
mem["ledger"] = [{"ts": i, "dt": "07-10 00:00", "ch": "C", "root": str(i), "mode": "initial",
                  "said": "s", "action": "answer", "reply": "r"} for i in range(130)]
runtime.save_json(convo.MEM_FILE, mem)
fake_llm.next_out = json.dumps({"action": "answer", "reply": "y"})
convo.decide("C0BCE19BN2G", "555.0", {"text": "z"}, mode="initial")
convo.commit()
check("ring cap 120", len(convo.memory()["ledger"]) == convo.LEDGER_CAP)

# 6. 蒸留: 検証ゲート
sys.path.insert(0, "/Users/malus_bot/Claude/Hermes/profile/skills/lipple/convo-memory/scripts")
import run as cm  # noqa: E402
mem = convo.memory()
mem["notes"] = [{"note": "既存の記憶", "kind": "決定", "since": "2026-07-01"}]
mem["distilled_ts"] = 0
runtime.save_json(convo.MEM_FILE, mem)
fake_llm.next_out = "こわれた"
check("distill bad output keeps notes", "温存" in cm.distill()
      and convo.memory()["notes"][0]["note"] == "既存の記憶")
fake_llm.next_out = json.dumps({"notes": []})
check("distill refuses full wipe", "温存" in cm.distill()
      and convo.memory()["notes"][0]["note"] == "既存の記憶")
long_note = "あ" * 200
fake_llm.next_out = json.dumps({"notes": [{"note": long_note, "kind": "へんな種別"}]
                                        + [{"note": f"n{i}", "kind": "好み"} for i in range(40)]})
msg = cm.distill()
mem = convo.memory()
check("distill ok", msg.startswith("ok"))
check("note truncated to 120", len(mem["notes"][0]["note"]) == 120)
check("unknown kind -> 注意", mem["notes"][0]["kind"] == "注意")
check("notes capped 25", len(mem["notes"]) == convo.NOTES_CAP)
check("distilled_ts = last fresh ts", mem["distilled_ts"] == max(
    float(e["ts"]) for e in mem["ledger"]))
check("distill skip when no fresh", cm.distill().startswith("skip: 新しい会話なし"))

print(f"\n{ok} checks passed")
