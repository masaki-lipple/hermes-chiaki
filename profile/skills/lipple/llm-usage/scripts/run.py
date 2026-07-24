#!/usr/bin/env python3
"""llm-usage（R5コスト計測・2026-07-24 戸田「R5」＝Issue「4. R5 コスト計測」）:
llm_usage.jsonl（lib/llm.pyが全呼び出しを計測）を集計して「1枚」にする。決定論・LLM非起動。
引数なし=標準出力のみ／`post`=#8902へ投稿／`post 30`=日数指定。"""
from __future__ import annotations

import datetime as dt
import os
import statistics
import sys
from collections import defaultdict
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source  # noqa: E402


def summarize(days: float = 7) -> str:
    """呼び出し元×モデルの集計テキスト（トーン規約準拠＝そのまま投稿できる形）。"""
    since = runtime.now_ts() - days * 86400
    rows = [r for r in runtime.read_jsonl("llm_usage.jsonl") if float(r.get("ts") or 0) > since]
    if not rows:
        return (f"直近{int(days)}日のLLM呼び出しの記録はまだありません"
                "（計測は2026-07-24開始＝データがたまってから見てください）。")
    by = defaultdict(lambda: {"n": 0, "fail": 0, "ms": [], "chars": 0})
    day_n = defaultdict(int)
    for r in rows:
        key = (r.get("caller") or "?", r.get("model") or "?")
        s = by[key]
        s["n"] += 1
        s["fail"] += 0 if r.get("ok") else 1
        s["ms"].append(float(r.get("ms") or 0))
        s["chars"] += int(r.get("in") or 0) + int(r.get("out") or 0)
        day_n[dt.datetime.fromtimestamp(float(r["ts"]),
                                        dt.timezone(dt.timedelta(hours=9))).strftime("%m-%d")] += 1
    lines = [f"LLM呼び出しの棚卸し（直近{int(days)}日・全{len(rows)}回・"
             f"1日平均{len(rows) / max(days, 1):.1f}回）。"]
    total_by_model = defaultdict(int)
    for (caller, model), s in sorted(by.items(), key=lambda x: -x[1]["n"]):
        total_by_model[model] += s["n"]
        med = statistics.median(s["ms"]) / 1000 if s["ms"] else 0
        fail = f"・失敗{s['fail']}" if s["fail"] else ""
        lines.append(f"• {caller}×{model}: {s['n']}回{fail}・中央値{med:.1f}秒・{s['chars'] // 1000}千字")
    lines.append("")
    lines.append("モデル別合計: " + "／".join(f"{m}={n}回" for m, n in
                                        sorted(total_by_model.items(), key=lambda x: -x[1])) + "。")
    lines.append("日別: " + "／".join(f"{d}={n}回" for d, n in sorted(day_n.items())) + "。")
    lines.append("参考: GPTはChatGPTサブスク内（追加課金なし）・Haiku/OpusはAPI課金。")
    return "\n".join(lines)


def main() -> None:
    days = 7.0
    post = len(sys.argv) > 1 and sys.argv[1] == "post"
    if len(sys.argv) > 2:
        try:
            days = float(sys.argv[2])
        except ValueError:
            pass
    text = summarize(days)
    print(f"[llm-usage]\n{text}")
    if post:
        r = source.post_message(runtime.CH_CHIAKI_MGMT, f"<@{runtime.TODA}>\n{text}")
        print(f"[llm-usage] posted={bool(isinstance(r, dict) and r.get('ts'))}")


if __name__ == "__main__":
    main()
