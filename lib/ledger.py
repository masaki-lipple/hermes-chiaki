"""実行台帳（再設計R1・2026-07-14 戸田GO・docs/REDESIGN.md）。

「1依頼（発話）=1行」の正本。event-sourcing型＝ state/exec_ledger.jsonl へ追記だけを行い、
同一 id の新しい行が古い行のフィールドを上書きする（読み手は load() でマージ済みを得る）。
追記は O_APPEND の1行書き＝プロセス間（listener/cron並走）でアトミック。コンパクション無し。

R1では「記録と突き合わせ（self-health）」のみ＝各スキルの挙動は変えない。
R2でここが所有権判定（誰の領分か）の正本になる。
"""
from __future__ import annotations

import json
import os

from lib import runtime

FILE = "exec_ledger.jsonl"
OWNERS = ("intake", "apply", "codex", "none")
# status の目安: received → processing → replied/filed/ruled/queued/skipped/failed


def event_id(ch: str, ts: str) -> str:
    return f"{ch}:{ts}"


def record(eid: str, **fields) -> None:
    """部分更新の追記。全フィールド任意（同idの過去行とマージされる）。失敗しても本処理
    （返信・起票）は止めないが、無音にはしない＝R3以降この記録は裁定の冪等ガードの根拠でもあり、
    静かに消えると多重実行の防御が失われる（2026-07-21 監査⑥a: fail-openとR3の矛盾の解消）。"""
    try:
        row = {"id": eid, "at": runtime.now_ts()}
        row.update({k: v for k, v in fields.items() if v is not None})
        runtime.append_jsonl(FILE, row)
    except Exception as e:
        try:
            print(f"[ledger] record失敗 {eid}: {type(e).__name__}: {e}")
        except Exception:
            pass


def load() -> dict:
    """{id: マージ済み状態}。新しい行が優先。"""
    out: dict = {}
    for row in runtime.read_jsonl(FILE):
        eid = row.get("id")
        if not eid:
            continue
        cur = out.setdefault(eid, {})
        cur.update({k: v for k, v in row.items() if k != "id"})
    return out


def entry(eid: str) -> dict:
    return load().get(eid, {})


def compact(keep_sec: float = 14 * 86400) -> int:
    """台帳の折り畳み（2026-07-24 Issue「10. 運用の磨き」＝追記型で肥大化するため月次相当で圧縮）。
    マージ後の最終更新（at）が keep_sec より古い id は「マージ済み1行」に畳み、新しい id の行は
    生のまま残す（イベントの粒度を保つ）。書き換え中に並走プロセスが追記した末尾行は読み直して
    再追記＝取りこぼさない（O_APPENDの1行書き前提）。戻り値=削れた行数。"""
    p = runtime.STATE_DIR / FILE
    if not p.exists():
        return 0
    size0 = p.stat().st_size
    rows = runtime.read_jsonl(FILE)
    now = runtime.now_ts()
    merged: dict = {}
    order: list = []
    for row in rows:
        eid = row.get("id")
        if not eid:
            continue
        if eid not in merged:
            merged[eid] = {"id": eid}
            order.append(eid)
        merged[eid].update({k: v for k, v in row.items() if k != "id"})
    out, kept_ids = [], set()
    for eid in order:
        if now - float(merged[eid].get("at") or 0) > keep_sec:
            out.append(json.dumps(merged[eid], ensure_ascii=False))
        else:
            kept_ids.add(eid)
    for row in rows:
        if row.get("id") in kept_ids:
            out.append(json.dumps(row, ensure_ascii=False))
    tmp = str(p) + ".compact"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + ("\n" if out else ""))
    with open(p, "rb") as f:  # 書き換え中の追記を回収
        f.seek(size0)
        tail = f.read().decode("utf-8", "replace")
    if tail.strip():
        with open(tmp, "a", encoding="utf-8") as f:
            f.write(tail if tail.endswith("\n") else tail + "\n")
    os.replace(tmp, p)
    return max(0, len(rows) - len(out))
