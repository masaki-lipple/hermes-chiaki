"""実行台帳（再設計R1・2026-07-14 戸田GO・docs/REDESIGN.md）。

「1依頼（発話）=1行」の正本。event-sourcing型＝ state/exec_ledger.jsonl へ追記だけを行い、
同一 id の新しい行が古い行のフィールドを上書きする（読み手は load() でマージ済みを得る）。
追記は O_APPEND の1行書き＝プロセス間（listener/cron並走）でアトミック。コンパクション無し。

R1では「記録と突き合わせ（self-health）」のみ＝各スキルの挙動は変えない。
R2でここが所有権判定（誰の領分か）の正本になる。
"""
from __future__ import annotations

from lib import runtime

FILE = "exec_ledger.jsonl"
OWNERS = ("intake", "apply", "codex", "none")
# status の目安: received → processing → replied/filed/ruled/queued/skipped/failed


def event_id(ch: str, ts: str) -> str:
    return f"{ch}:{ts}"


def record(eid: str, **fields) -> None:
    """部分更新の追記。全フィールド任意（同idの過去行とマージされる）。失敗は握りつぶし＝
    台帳は観測手段であり、本処理（返信・起票）を台帳都合で止めない。"""
    try:
        row = {"id": eid, "at": runtime.now_ts()}
        row.update({k: v for k, v in fields.items() if v is not None})
        runtime.append_jsonl(FILE, row)
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
