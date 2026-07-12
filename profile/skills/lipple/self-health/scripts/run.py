#!/usr/bin/env python3
"""self-health: 毎朝の自己点検（2026-07-10 戸田「なぜ無視される」→再発防止としてGO）。
①listener生存 ②cron生存（前回点検以降のcron.log差分に各スキルの実行痕跡）③台帳鮮度
④listenerが受信・起動を記録したのに処理痕跡が無いイベント（黙殺）の検知。
異常があるときだけ#8902へ警告（正常時は無音＝ログのみ）。決定論・LLM非起動。
cron: 40 8 * * 1-5。
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import runtime, source  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
STATE = "self_health.json"
DISPATCH = "listener_dispatch.jsonl"
GRACE_SEC = 600  # 直近10分のイベントは処理中の可能性＝次回の点検に回す
LOG_EXPECT = {  # cron.logの実行痕跡（毎回必ず1行は出すスキルのみ。stall-scan/silenceは[SILENT]共用のため対象外）
    "chiaki-intake": "[intake]",
    "apply-ruling": "[apply-ruling]",
    "obs-batch": "[obs-batch]",
    "propose-to-approval": "[propose]",
    "typo-scan": "[typo-scan]",
    "chiaki-pdca": "[chiaki-pdca]",
    "sync-notation": "[sync]",
    "task-follow": "[task-follow]",
    "codex-runner": "[codex-runner]",
    "compute-baselines": "[compute-baselines]",
    "convo-memory": "[convo-memory]",
}


def _listener_alive() -> bool:
    try:
        r = subprocess.run(["systemctl", "--user", "is-active", "chiaki-listener.service"],
                           capture_output=True, text=True, timeout=15)
        return (r.stdout or "").strip() == "active"
    except Exception:
        return False


def _covers_full_workday(prev: float, now: float) -> bool:
    """点検窓 [prev, now] が「営業日の9:00〜21:30」を丸ごと含むか。含まない窓（週末に基準を
    作り直した直後など）では「全cronの痕跡があるはず」という期待自体が成り立たない＝誤警報になる。"""
    d = dt.datetime.fromtimestamp(prev, JST).date()
    end_d = dt.datetime.fromtimestamp(now, JST).date()
    while d <= end_d:
        noon = dt.datetime(d.year, d.month, d.day, 12, tzinfo=JST).timestamp()
        if runtime.is_jp_workday(noon):
            start = dt.datetime(d.year, d.month, d.day, 9, 0, tzinfo=JST).timestamp()
            end = dt.datetime(d.year, d.month, d.day, 21, 30, tzinfo=JST).timestamp()
            if prev <= start and end <= now:
                return True
        d += dt.timedelta(days=1)
    return False


def _log_missing(st: dict, now: float) -> list[str]:
    """前回点検以降のcron.log差分に、各スキルの痕跡があるか。初回は基準が無い＝オフセットだけ記録。"""
    p = Path(runtime.STATE_DIR) / "cron.log"
    if not p.exists():
        return ["cron.log が存在しない（cron自体が止まっている可能性）"]
    size = p.stat().st_size
    first = "log_offset" not in st
    off = int(st.get("log_offset") or 0)
    if off > size:  # ログが縮んだ（ローテーション等）＝先頭から
        off = 0
    prev = float(st.get("log_checked_ts") or 0)
    st["log_offset"] = size
    st["log_checked_ts"] = now
    # prev=0（旧版の状態ファイル等で基準時刻が無い）も「窓が不明」＝スキップ。
    # 2026-07-12 実発生: 旧版が作った基準に log_checked_ts が無く、ガードを素通りして誤警報11件を投稿した。
    if first or not prev or not _covers_full_workday(prev, now):
        return []  # 窓が営業日を丸ごと含まない＝期待が成り立たない（オフセットの前進のみ）
    with open(p, "rb") as f:
        f.seek(off)
        chunk = f.read().decode("utf-8", "replace")
    return [f"{name} の実行痕跡（{tag}）が前回点検以降のcron.logに無い"
            for name, tag in LOG_EXPECT.items() if tag not in chunk]


def _last_workday_9am(now: float) -> float:
    d = dt.datetime.fromtimestamp(now, JST).date() - dt.timedelta(days=1)
    while not runtime.is_jp_workday(dt.datetime(d.year, d.month, d.day, 12, tzinfo=JST).timestamp()):
        d -= dt.timedelta(days=1)
    return dt.datetime(d.year, d.month, d.day, 9, 0, tzinfo=JST).timestamp()


def _ledger_stale(now: float) -> list[str]:
    up = float(runtime.load_json("task_ledger.json", {}).get("updated_at") or 0)
    if up >= _last_workday_9am(now):
        return []
    when = dt.datetime.fromtimestamp(up, JST).strftime("%m-%d %H:%M") if up else "記録なし"
    return [f"stall-scan の台帳（task_ledger.json）が前営業日9時以降更新されていない（最終更新={when}）"]


def _swallowed(st: dict, now: float) -> list[str]:
    """listenerの受信・起動記録と処理痕跡の突き合わせ＝「受けたのに黙殺」の検知。
    2026-07-10 a040バグはこの型（listenerは起動・intakeは走査範囲外でnothing new）だった。"""
    last = float(st.get("dispatch_ts") or 0)
    horizon = now - GRACE_SEC
    rows = [r for r in runtime.read_jsonl(DISPATCH)
            if last < float(r.get("at") or 0) <= horizon]
    st["dispatch_ts"] = horizon
    if not rows:
        return []
    items = runtime.load_json("chiaki_intake.json", {"items": {}}).get("items", {})
    cur = runtime.load_json("tuning_cursor.json", {})
    codex = runtime.load_json("codex_threads.json", {"items": {}}).get("items", {})
    pend = runtime.load_json("pending_approvals.json", {"items": {}}).get("items", {})
    ruling_roots = set(pend) | {v.get("source_ts") for v in pend.values()}
    warns = []
    for r in rows:
        ch, ts, action = r.get("ch") or "", r.get("ts") or "", r.get("action") or ""
        thread = r.get("thread") or ""
        ok = True
        if action == "intake":
            if thread in ruling_roots:  # 裁定スレッド内の発話（GO等）はapply-rulingの領分＝対象外
                continue
            ok = ts in items or float(cur.get(ch, 0)) >= float(ts or 0)
        elif action == "codex":
            t = codex.get(thread)
            ok = (not t) or float(t.get("last_seen_ts") or 0) >= float(ts or 0)
        # action == "apply" は状態遷移が多岐＝ここでは監査しない
        if not ok:
            when = dt.datetime.fromtimestamp(float(r.get("at") or 0), JST).strftime("%m-%d %H:%M")
            warns.append(f"listenerが受信・起動したのに処理痕跡が無い: {when}のイベント"
                         f"（{action}・ch={ch}・ts={ts}）")
    return warns


def main() -> None:
    now = runtime.now_ts()
    st = runtime.load_json(STATE, {})
    warns = []
    if not _listener_alive():
        warns.append("chiaki-listener.service が active でない（即時応答が全停止＝要再起動）")
    warns += _log_missing(st, now)
    warns += _ledger_stale(now)
    warns += _swallowed(st, now)
    runtime.save_json(STATE, st)
    if not warns:
        print("[self-health] ok")
        return
    body = "\n".join(f"• {w}" for w in warns)
    text = (f"<@{runtime.TODA}>\n毎朝の自己点検で異常を検知しました。\n\n{body}\n\n"
            "Claude Codeのセッションで原因を調査・修正してください。")
    try:
        source.post_message(runtime.CH_CHIAKI_MGMT, text)
    except Exception as e:
        print(f"[self-health] post failed: {e}")
    print(f"[self-health] warns={len(warns)}")


if __name__ == "__main__":
    main()
