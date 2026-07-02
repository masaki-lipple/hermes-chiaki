#!/usr/bin/env python3
"""task-follow: task_ledger.json から確認待ち/期限当日の未報告を機械的に追う。
cron 例: 50 8 * * 1-5（--no-agent / --script）。LLM 非起動。
"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source  # noqa: E402


COMPLETE_WORDS = (
    "完了しました", "対応しました", "修正しました", "実施しました", "行いました", "確認しました",
    "終了します", "終了しました", "アップしました", "更新しました", "作成しました", "済です", "済みです",
)
PENDING_RE = re.compile(r"(?:し|行い|確認し|対応し)ます(?!た)|後ほど|あとで|のちほど|これから|予定です|再度確認")
MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")
BOT_USERS = {runtime.GCP_TASK_BOT, runtime.CHIAKI_SELF}
KEEP_DAYS = 45


def _today_jst(now: float) -> str:
    jst = dt.datetime.fromtimestamp(now, dt.timezone(dt.timedelta(hours=9)))
    return jst.strftime("%Y-%m-%d")


def _is_human_reply(m: dict, root_ts: str) -> bool:
    uid = m.get("user_id") or ""
    return m.get("ts") != root_ts and uid and uid not in BOT_USERS


def _is_complete_report(text: str) -> bool:
    return any(w in text for w in COMPLETE_WORDS) and not PENDING_RE.search(text)


def _latest_report(replies: list[dict], root_ts: str) -> dict | None:
    reports = [m for m in replies
               if _is_human_reply(m, root_ts) and _is_complete_report(m.get("text") or "")]
    if not reports:
        return None
    return max(reports, key=lambda m: float(m.get("ts_float") or m.get("ts") or 0))


def _uniq(items: list[str]) -> list[str]:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _report_mentions(report: dict) -> list[str]:
    author = report.get("user_id") or ""
    return _uniq([uid for uid in MENTION_RE.findall(report.get("text") or "")
                  if uid != author and uid not in BOT_USERS])


def _mentioned_replied_after(replies: list[dict], mentioned: list[str], report: dict) -> bool:
    targets = set(mentioned)
    report_ts = float(report.get("ts_float") or report.get("ts") or 0)
    for m in replies:
        if (m.get("user_id") in targets
                and float(m.get("ts_float") or m.get("ts") or 0) > report_ts):
            return True
    return False


def _sent_ts(value) -> float:
    if isinstance(value, dict):
        value = value.get("ts", 0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _cleanup_sent(sent: dict, now: float) -> dict:
    cutoff = now - KEEP_DAYS * 86400
    return {k: v for k, v in sent.items() if _sent_ts(v) >= cutoff}


def main():
    if not runtime.is_jp_workday():
        print("[task-follow] holiday/weekend")
        return

    now = runtime.now_ts()
    today = _today_jst(now)
    ledger = runtime.load_json("task_ledger.json", {}).get("tasks", {})
    sent = runtime.load_json("task_follow.json", {})
    counts = {"A": 0, "B": 0, "skip_kanryo": 0, "read_error": 0}

    for t in ledger.values():
        ch = t.get("channel")
        root_ts = t.get("ts")
        if not ch or not root_ts:
            continue
        if "kanryo" in (t.get("reactions") or []):
            counts["skip_kanryo"] += 1
            continue
        try:
            replies = source.read_thread(ch, root_ts)
        except Exception as e:
            counts["read_error"] += 1
            print(f"[task-follow] read_thread failed ch={ch} ts={root_ts}: {e}")
            continue

        report = _latest_report(replies, root_ts)
        if report:
            mentioned = _report_mentions(report)
            if not mentioned:
                continue
            report_date = (report.get("datetime") or "")[:10]
            if report_date and report_date < today and not _mentioned_replied_after(replies, mentioned, report):
                key = f"A:{ch}:{root_ts}:{report.get('ts')}"
                if key not in sent:
                    mentions = " ".join(f"<@{uid}>" for uid in mentioned)
                    source.post_thread_reply(ch, root_ts, f"{mentions}\n報告の確認をお願いします！")
                    sent[key] = {"ts": now}
                    counts["A"] += 1
            continue

        due = t.get("due")
        assignees = _uniq(t.get("assignees") or [])
        if due == today and assignees:
            key = f"B:{ch}:{root_ts}:{due}"
            if key not in sent:
                mentions = " ".join(f"<@{uid}>" for uid in assignees)
                source.post_thread_reply(ch, root_ts, f"{mentions}\n本日が対応期限です。状況の報告をお願いします！")
                sent[key] = {"ts": now}
                counts["B"] += 1

    runtime.save_json("task_follow.json", _cleanup_sent(sent, now))
    print("[task-follow] A={A} B={B} skip_kanryo={skip_kanryo} read_error={read_error}".format(**counts))


if __name__ == "__main__":
    main()
