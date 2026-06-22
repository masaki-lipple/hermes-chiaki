#!/usr/bin/env python3
"""typo-scan の対象メッセージを集める（LLM が読む素材を用意するだけ。判定は agent/Haiku）。
使い方: gather.py            → 当日分（17:50想定）
        gather.py --since TS → TS 以降の差分のみ（18:30想定）
出力: JSON {date, messages:[{ts,datetime,text}]} を stdout。
"""
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=float, default=None)
    ap.add_argument("--channel", default=runtime.CH_YU_PDCA)
    args = ap.parse_args()

    recent = source.read_recent(args.channel, oldest_ts=args.since, limit=200)
    if not recent:
        print(json.dumps({"messages": []}))
        return
    today = recent[-1]["datetime"][:10]
    msgs = [m for m in recent if m["datetime"][:10] == today and (args.since is None or m["ts_float"] > args.since)]
    # 報告本文だけ（chiaki/bot 等は対象外。ここでは投稿者を絞らずそのまま渡し、agent が判断）
    out = [{"ts": m["ts"], "datetime": m["datetime"], "text": m["text"]} for m in msgs]
    print(json.dumps({"date": today, "channel": args.channel, "messages": out},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
