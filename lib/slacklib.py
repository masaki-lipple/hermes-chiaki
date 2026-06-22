"""Slack 取り込みの正規化レイヤ。

本番(VPS)では Slack MCP / Web API がメッセージを返す。ローカル検証では、MCP の
slack_read_channel が保存した「detailed テキスト形式」のダンプ(JSON: {messages, pagination_info})
を正規化メッセージ dict に変換して fixtures を作る。

正規化メッセージ dict:
  {
    "ts": "1782118906.964539",   # Slack message ts（実測工数・無音判定の正)
    "ts_float": 1782118906.964539,
    "datetime": "2026-06-22 18:01:46 JST",
    "user_id": "U09T44VEZM1",
    "user_name": "Yu Matsunaga",
    "text": "本文（複数行可・前後の <!channel> 等は残す）",
    "thread_replies": 3 or None,
    "thread_latest": "2026-06-22 14:16:55 JST" or None,
    "has_files": True/False,
  }

観測スクリプトはこの正規化 dict だけを入力に取る（Slack ソースに依存しない）。
"""
from __future__ import annotations
import json
import re
from pathlib import Path

_BLOCK_RE = re.compile(
    r"=== Message from (?P<name>.+?) \((?P<uid>[A-Z0-9]+)\) at (?P<dt>.+?) ===\s*\n"
    r"Message TS:\s*(?P<ts>\d+\.\d+)\s*\n"
    r"(?P<body>.*?)(?=\n=== Message from |\Z)",
    re.DOTALL,
)
_THREAD_RE = re.compile(r"^Thread:\s*(\d+)\s*repl.*?(?:latest:\s*(.+?))?\)?\s*$")
_FILES_RE = re.compile(r"^Files:\s")
_REACT_RE = re.compile(r"^Reactions:\s")


def _load_messages_string(path: str | Path) -> tuple[str, str | None]:
    raw = Path(path).read_text(encoding="utf-8")
    try:
        obj = json.loads(raw)
        msgs = obj.get("messages", "")
        cursor = None
        pi = obj.get("pagination_info") or ""
        m = re.search(r"cursor:\s*`([^`]+)`", pi)
        if m:
            cursor = m.group(1)
        return msgs, cursor
    except json.JSONDecodeError:
        return raw, None


def parse_dump(path: str | Path) -> list[dict]:
    """1 つのダンプファイルを正規化メッセージのリストに変換。"""
    msgs, _ = _load_messages_string(path)
    out = []
    for m in _BLOCK_RE.finditer(msgs):
        body_lines = m.group("body").splitlines()
        text_lines, thread_replies, thread_latest, has_files = [], None, None, False
        for ln in body_lines:
            tl = ln.strip()
            tm = _THREAD_RE.match(tl)
            if tm:
                thread_replies = int(tm.group(1))
                thread_latest = (tm.group(2) or "").strip() or None
                continue
            if _FILES_RE.match(tl):
                has_files = True
                continue
            if _REACT_RE.match(tl):
                continue
            text_lines.append(ln)
        text = "\n".join(text_lines).strip()
        out.append({
            "ts": m.group("ts"),
            "ts_float": float(m.group("ts")),
            "datetime": m.group("dt").strip(),
            "user_id": m.group("uid"),
            "user_name": m.group("name").strip(),
            "text": text,
            "thread_replies": thread_replies,
            "thread_latest": thread_latest,
            "has_files": has_files,
        })
    return out


def build_fixture(dump_paths: list[str], out_path: str | Path) -> list[dict]:
    """複数ダンプをマージ・ts重複除去・昇順ソートして fixture JSON を書き出す。"""
    by_ts: dict[str, dict] = {}
    for p in dump_paths:
        for msg in parse_dump(p):
            by_ts[msg["ts"]] = msg
    merged = sorted(by_ts.values(), key=lambda x: x["ts_float"])
    Path(out_path).write_text(
        json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return merged


if __name__ == "__main__":
    import sys
    msgs = build_fixture(sys.argv[2:], sys.argv[1])
    print(f"wrote {len(msgs)} messages -> {sys.argv[1]}")
    if msgs:
        print(f"range: {msgs[0]['datetime']}  ..  {msgs[-1]['datetime']}")
