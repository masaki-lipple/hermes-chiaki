#!/usr/bin/env python3
"""ledger-notion: 実行台帳をExecution_Chiaki_AI_DB（Notion）へ日次同期（決定論・LLM非起動）。
cron: 40 21 * * 1-5。正本はローカル・NotionはIDで冪等に追記/更新する閲覧用の控え。"""
from __future__ import annotations

import datetime as dt
import os
import re
import sys
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import convo, ledger, notion, runtime, source  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))
ST_JP = {"handled": "処理済み", "ruled": "裁定済み", "ok": "実行成功", "failed": "失敗",
         "deferred": "保留", "skipped": "スキップ", "received": "受信", "queued": "キュー投入",
         "ruling": "実行中"}  # R3⑤の消費開始クレーム（2026-07-23 レビュー: 未登録で生英語が出ていた）
OWN_JP = {"intake": "窓口", "apply": "裁定", "codex": "Codex"}
PRI = ["ruled", "handled", "ok", "failed", "ruling", "deferred", "queued", "skipped", "received"]
UPDATE_WINDOW_SEC = 14 * 86400  # これより古い依頼は不変扱い（更新チェックしない）


def _lag(sec: float) -> str:
    return f"+{int(sec)}s" if sec < 120 else (f"+{int(sec / 60)}分" if sec < 7200 else f"+{sec / 3600:.1f}h")


def summarize(events: list) -> dict:
    """1依頼ぶんの台帳イベント列 → Notion行の素材（結果状態・遷移・応答秒数など）。"""
    es = sorted(events, key=lambda r: r.get("at") or 0)
    merged = {}
    for r in es:
        merged.update({k: v for k, v in r.items() if v is not None})
    sts = [r.get("status") for r in es]
    outcome = next((s for s in PRI if s in sts), "received")
    trans, first_at = [], es[0].get("at") or 0
    for r in es:
        st = r.get("status")
        if trans and trans[-1][0] == st:
            trans[-1][1] += 1
        else:
            trans.append([st, 1, r.get("at") or 0])
    tstr = "→".join(f"{ST_JP.get(st, st)}{'×' + str(n) if n > 1 else ''}({_lag(at - first_at)})"
                    for st, n, at in trans)
    rec = next((r for r in es if r.get("status") == "received"), None)
    han = next((r for r in es if r.get("status") in ("handled", "ruled")), None)
    latency = round(han["at"] - rec["at"]) if rec and han and han["at"] > rec["at"] else None
    ts = merged.get("ts") or ""
    text = re.sub(r"<@U[A-Z0-9]+>", "", merged.get("text") or "").replace("&gt;", ">")
    text = text.replace("\n", " ").strip()
    refs = merged.get("refs") or {}
    if not text:
        text = (f"Codex実行（{refs.get('branch', '?')}）" if merged.get("owner") == "codex"
                else "（本文記録なし）")
    ch, root = merged.get("ch") or "", merged.get("thread_root") or ""
    link = ""
    if ch and ts:
        link = (f"https://lipple.slack.com/archives/{ch}/p{ts.replace('.', '')}"
                + (f"?thread_ts={root}&cid={ch}" if root else ""))
    elif ch and root:
        link = f"https://lipple.slack.com/archives/{ch}/p{root.replace('.', '')}"
    note = " / ".join(sorted({r.get("note") for r in es
                              if r.get("note") and r.get("note") != "already_replied"
                              and not r.get("note", "").startswith("消費開始クレーム")}))
    if refs.get("approval"):
        ap = refs["approval"]
        note = (note + " / " if note else "") + f"承認digest={ap.get('digest')}({ap.get('verdict')})"
    if refs.get("branch"):
        note = (note + " / " if note else "") + f"branch={refs['branch']}"
    return {"first_at": first_at, "title": text[:100], "outcome": ST_JP.get(outcome, outcome),
            "owner": OWN_JP.get(merged.get("owner"), merged.get("owner") or "—"),
            "actor": merged.get("actor") or "", "ch": ch, "trans": tstr,
            "latency": latency, "link": link, "note": note[:180]}


def _props(eid: str, s: dict, ch_names: dict, actor_names: dict) -> dict:
    def rt(v):
        return {"rich_text": [{"text": {"content": v}}]} if v else {"rich_text": []}
    p = {"発話": {"title": [{"text": {"content": s["title"]}}]},
         "日時": {"date": {"start": dt.datetime.fromtimestamp(s["first_at"], JST)
                          .strftime("%Y-%m-%dT%H:%M:%S+09:00")}},
         "チャンネル": rt(ch_names.get(s["ch"], s["ch"] or "—")),
         "発話者": rt(actor_names.get(s["actor"], s["actor"] or "—")),
         "担当": {"select": {"name": s["owner"]}} if s["owner"] != "—" else {"select": None},
         "状態": {"select": {"name": s["outcome"]}},
         "遷移": rt(s["trans"]), "ID": rt(eid), "備考": rt(s["note"])}
    if s["latency"] is not None:
        p["応答秒数"] = {"number": s["latency"]}
    if s["link"]:
        p["Slackリンク"] = {"url": s["link"]}
    return p


def _existing_rows() -> dict | None:
    """DBの既存行 {ID: {"page_id", "trans", "outcome"}}。API失敗/未共有は None。"""
    out, cursor = {}, None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        res = notion._api("POST", f"databases/{notion.EXEC_LEDGER_DB}/query", body)
        if res is None:
            return None
        for pg in res.get("results", []):
            pr = pg.get("properties", {})
            eid = "".join(t.get("plain_text", "") for t in (pr.get("ID", {}).get("rich_text") or []))
            if eid:
                out[eid] = {
                    "page_id": pg["id"],
                    "trans": "".join(t.get("plain_text", "") for t in (pr.get("遷移", {}).get("rich_text") or [])),
                    "actor": "".join(t.get("plain_text", "") for t in (pr.get("発話者", {}).get("rich_text") or [])),
                    "outcome": ((pr.get("状態", {}).get("select") or {}).get("name")) or ""}
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return out


def main() -> None:
    if not notion._token():
        print("[ledger-notion] no token -> skip")
        return
    existing = _existing_rows()
    if existing is None:
        print("[ledger-notion] DBへアクセス不可（Hermes Agentへの共有待ち or API障害）-> skip")
        return
    events: dict = {}
    for r in runtime.read_jsonl(ledger.FILE):
        if r.get("id"):
            events.setdefault(r["id"], []).append(r)
    ch_names = {}
    try:
        ch_names = {c["id"]: c.get("name") or "" for c in source.list_bot_channels()}
    except Exception:
        pass
    actor_names = {}
    now = runtime.now_ts()
    created = updated = 0
    for eid, es in events.items():
        s = summarize(es)
        if s["actor"] and s["actor"] not in actor_names:
            # 発話者は固定表記（convo.NAMES）を最優先＝Slack表示名の揺らぎをDBへ持ち込まない
            # （2026-07-23 戸田「発話者がゆらいでいる、Masaki Toda〜といった表記で固定」）
            actor_names[s["actor"]] = (convo.NAMES.get(s["actor"])
                                       or source.user_display_name(s["actor"]) or s["actor"])
        cur = existing.get(eid)
        if cur is None:
            if notion._create_page(notion.EXEC_LEDGER_DB, _props(eid, s, ch_names, actor_names),
                                   "exec-ledger"):
                created += 1
            continue
        changed = (now - s["first_at"] < UPDATE_WINDOW_SEC
                   and (cur["trans"] != s["trans"] or cur["outcome"] != s["outcome"]))
        # 発話者の表記が固定表記とズレている行は14日窓に関係なく直す（既存行の揺らぎも一掃）
        drift = cur.get("actor", "") != actor_names.get(s["actor"], s["actor"] or "—")
        if changed or drift:
            if notion.update_page_props(cur["page_id"], _props(eid, s, ch_names, actor_names)):
                updated += 1
    print(f"[ledger-notion] 追加={created} 更新={updated} 既存={len(existing)}")
    try:
        # 台帳の圧縮（2026-07-24 Issue「10. 運用の磨き」）: 行数が閾値を超えたら古いidを折り畳む。
        # 同期の後＝この日次cron（21:40）は営業時間外で並走追記がほぼ無い時間帯
        if len(runtime.read_jsonl(ledger.FILE)) > 4000:
            print(f"[ledger-notion] 台帳コンパクション: {ledger.compact()}行を折り畳み")
    except Exception as e:
        print(f"[ledger-notion] compact失敗（続行）: {e}")


if __name__ == "__main__":
    main()
