#!/usr/bin/env python3
"""引用除外+複合語ガード+切れ指摘禁止（2026-07-29 戸田「実際に発言した人に…」「これまた発生している」）のテスト。
実例=7/3提案: クライアント引用文の「事業所」を「事→こと」で誤検知（旧仕様なら宛先も投稿者でなかった）。
7/28提案: 「以下工数表です。」の「以下工」を「入力途中」と幻覚検知。"""
import os
import sys
import types
from pathlib import Path

SCRATCH = Path(__file__).parent / "state_qg"
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

from lib import observe, runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1

RULES = {"terms": [], "acronyms": [], "style_rules": [{"wrong": "事", "right": "こと", "rule": "r1"}]}

def founds(text):
    return [i["found"] for i in observe.notation_check(text, RULES)]

# ── 1文字漢字ガード: 直前仮名+直後が漢字でない用法だけ拾う ──
check("formal-noun use detected", founds("前に使った事があるはず。") == ["事"])
check("compound after kana skipped (の事業所)", founds("他の事業所も同じ金額です。") == [])
check("compound after kanji skipped (記事)", founds("記事を書きました。") == [])
check("sentence-end formal noun detected", founds("それは知らなかった事。") == ["事"])

# ── 引用ブロック除外 ──
check("quoted line skipped (>)", founds("> 前に使った事があるはず。") == [])
check("quoted line skipped (&gt;)", founds("&gt; 前に使った事があるはず。") == [])
check("own text next to quote still detected",
      founds("> 引用の事業所の話。\nこちらで確認した事があります。") == ["事"])

# ── typo-scan: 引用除外+切れ指摘禁止 ──
T = f"{REPO}/profile/skills/lipple/typo-scan/scripts/run.py"
gt = {"__file__": T, "__name__": "typo_mod"}
exec(compile(open(T).read(), T, "exec"), gt)
check("plain strips quote lines",
      gt["_plain"]("本文です\n> 引用ですです\n&gt; 引用2\n続き") == "本文です\n続き")

seen = {}
def cap_haiku(user, system=None, max_tokens=0):
    seen["sys"] = system
    seen["user"] = user
    return "[]"
fake_llm.haiku = cap_haiku
gt["_detect"]([{"text": "本文\n> 引用ですです"}], ["Lipple"])
check("truncation claims prohibited in prompt", "切れ" in seen["sys"] and "入力途中" in seen["sys"])
check("quotes not sent to detector", "引用ですです" not in seen["user"])

# found が引用行にしか無い検知は捨てる
source.list_bot_channels = lambda: [{"id": "CX", "name": "x"}]
now = runtime.now_ts()
gt["_gather"] = lambda ch, since, bots: ([{"ts": "1.0", "ts_float": now, "datetime": "d",
                                           "text": "本文は正しい。\n> 引用ですです", "user_id": "U1"}], now)
gt["_detect"] = lambda msgs, known: [{"i": 0, "found": "ですです", "suggest": "です"}]
gt["main"]()
check("found only in quote dropped", runtime.read_jsonl("findings.jsonl") == [])

print(f"\n{ok} checks passed")
