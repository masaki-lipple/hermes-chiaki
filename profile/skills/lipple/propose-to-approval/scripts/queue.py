#!/usr/bin/env python3
"""findings / pending_approvals の機械的 bookkeeping（判断は agent が SKILL.md に従って行う）。
  queue.py list                         → status:new の findings を JSON で
  queue.py mark <idx> <status>          → findings[idx] の status 更新（proposed/skipped/...）
  queue.py pending <idx> <pts> <ch> <sts> <draft>  → pending_approvals に記録
"""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime  # noqa: E402

F = "findings.jsonl"


def _all():
    return runtime.read_jsonl(F)


def _save_all(rows):
    runtime.STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(runtime.STATE_DIR / F, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def main(argv):
    cmd = argv[0] if argv else "list"
    if cmd == "list":
        rows = _all()
        new = [{"idx": i, **r} for i, r in enumerate(rows) if r.get("status") == "new"]
        print(json.dumps(new, ensure_ascii=False, indent=2))
    elif cmd == "mark":
        idx, status = int(argv[1]), argv[2]
        rows = _all()
        rows[idx]["status"] = status
        _save_all(rows)
        print(f"marked {idx} -> {status}")
    elif cmd == "pending":
        idx, pts, ch, sts, draft = int(argv[1]), argv[2], argv[3], argv[4], argv[5]
        pend = runtime.load_json("pending_approvals.json", {"items": {}})
        pend.setdefault("items", {})[pts] = {
            "finding_idx": idx, "source_channel": ch, "source_thread_ts": sts,
            "draft_text": draft, "status": "pending"}
        runtime.save_json("pending_approvals.json", pend)
        print(f"pending recorded: {pts}")
    else:
        print("usage: list | mark <idx> <status> | pending <idx> <pts> <ch> <sts> <draft>")


if __name__ == "__main__":
    main(sys.argv[1:])
