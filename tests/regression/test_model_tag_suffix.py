#!/usr/bin/env python3
"""モデル表記のテスト（2026-07-21 戸田最終指定: テキスト終わり=文末に続ける/URL終わり=独立行）。"""
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRATCH = Path(__file__).parent / "state_model_tag"
import shutil; shutil.rmtree(SCRATCH, ignore_errors=True)
(SCRATCH / "state").mkdir(parents=True, exist_ok=True)
os.environ["HERMES_PROFILE_DIR"] = str(SCRATCH)
os.environ["HERMES_LIB"] = str(ROOT)
sys.path.insert(0, str(ROOT))

fake_llm = types.ModuleType("lib.llm")
fake_llm.gpt = lambda *a, **k: ""
fake_llm.haiku = lambda *a, **k: ""
fake_llm.reset_used = lambda: None
fake_llm.last_used = lambda: "GPT"
sys.modules["lib.llm"] = fake_llm

from lib import runtime, source  # noqa: E402

ok = 0
def check(name, cond):
    global ok
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        sys.exit(1)
    ok += 1


check("inline duplicate collapsed",
      runtime.append_model_tag("特定できません。（GPT）\n（GPT）", "GPT") == "特定できません。（GPT）")
check("missing punctuation restored",
      runtime.append_model_tag("特定できません（GPT）", "GPT") == "特定できません。（GPT）")
check("tag goes after trailing url",
      runtime.append_model_tag("登録しました！（GPT）\nhttps://app.notion.com/p/abc", "GPT")
      == "登録しました！\nhttps://app.notion.com/p/abc\n（GPT）")
check("plain text inline (改行なし)",
      runtime.append_model_tag("了解です！", "GPT 5.5") == "了解です！（GPT 5.5）")

posted = []
source.post_thread_reply = lambda ch, ts, text: posted.append((ch, ts, text)) or {"ok": True, "ts": "77.7"}

I = str(ROOT / "profile/skills/lipple/chiaki-intake/scripts/run.py")
gi = {"__file__": I, "__name__": "intake_mod"}
exec(compile(open(I).read(), I, "exec"), gi)
gi["_reply"]("C1", "10.0", "特定できません（GPT）")
out = posted[-1][2]
check("intake tag inline", "特定できません。（GPT）" in out and "\n（GPT）" not in out)
check("intake tag single", out.count("（GPT）") == 1)

posted.clear()
gi["_reply"]("C1", "10.0", "登録しました！（GPT）", "https://app.notion.com/p/abc")
out = posted[-1][2]
check("intake tag after trailing url",
      "登録しました！\nhttps://app.notion.com/p/abc\n（GPT）" in out)
check("intake url tag single", out.count("（GPT）") == 1)

C = str(ROOT / "profile/skills/lipple/codex-runner/scripts/run.py")
gc = {"__file__": C, "__name__": "codex_mod"}
exec(compile(open(C).read(), C, "exec"), gc)
out = gc["_fmt"]("特定できません。（GPT）")
check("codex-runner tag inline", "特定できません。（GPT）" in out and "\n（GPT）" not in out)
check("codex-runner tag single", out.count("（GPT）") == 1)

print(f"\n{ok} checks passed")
