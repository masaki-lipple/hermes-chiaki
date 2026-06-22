#!/usr/bin/env python3
"""裁定の機械的 bookkeeping（分類・文面は agent が SKILL.md に従う）。
  apply.py find <thread_ts>                         → 該当 pending を JSON で
  apply.py resolve <thread_ts> <verdict> "<final>"  → pending 解消＋ログ
  apply.py memo "<一行>"                              → MEMORY.md に追記（昇格メモ）
"""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime  # noqa: E402


def main(argv):
    cmd = argv[0] if argv else ""
    if cmd == "find":
        pend = runtime.load_json("pending_approvals.json", {"items": {}})
        item = pend.get("items", {}).get(argv[1])
        print(json.dumps(item, ensure_ascii=False, indent=2) if item else "null")
    elif cmd == "resolve":
        tts, verdict, final = argv[1], argv[2], (argv[3] if len(argv) > 3 else "")
        pend = runtime.load_json("pending_approvals.json", {"items": {}})
        it = pend.get("items", {}).get(tts)
        if not it:
            print("no pending for", tts)
            return
        it["status"] = verdict
        it["final_text"] = final
        runtime.save_json("pending_approvals.json", pend)
        runtime.append_jsonl("rulings.jsonl", {"ts": runtime.now_ts(), "thread_ts": tts,
                                               "verdict": verdict, "final_text": final, **it})
        print(f"resolved {tts} -> {verdict}")
    elif cmd == "memo":
        line = argv[1]
        p = runtime.PROFILE_DIR / "MEMORY.md"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n- (昇格メモ) {line}\n")
        print("memo appended")
    else:
        print("usage: find <tts> | resolve <tts> <verdict> <final> | memo <line>")


if __name__ == "__main__":
    main(sys.argv[1:])
