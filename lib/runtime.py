"""プロファイル状態の読み書き・パス解決・findings/proposal 補助。

state/*.json は本番(box)で skill scripts が読み書きする運用状態。
時刻は実時間でよい（cron 実行）。Workflow スクリプトではないので Date 制限なし。
"""
from __future__ import annotations
import json
import os
import re
import time
from pathlib import Path

# profile dir: env 優先（box: ~/.hermes/profiles/management）、無ければリポジトリの profile/
PROFILE_DIR = Path(os.environ.get("HERMES_PROFILE_DIR")
                   or Path(__file__).resolve().parents[1] / "profile")
STATE_DIR = PROFILE_DIR / "state"

# 監視/発信チャンネル
CH_YU_PDCA = "C09U4T1BBU0"
CH_NICHIJI = "C045C1ZBX26"
CH_CHIAKI_PDCA = "C0BC6PPG013"
CH_CHIAKI_MGMT = "C0BCE19BN2G"
TODA = "U9R35H06L"
GCP_TASK_BOT = "U0BBZ3B3UNS"
CHIAKI_SELF = "U0BCCMPKD54"  # 新Bot「Chiaki AI」の user_id。処理・独り言はこれでセルフメンション


def now_ts() -> float:
    return time.time()


# 日本の祝日（内閣府暦・静的定数）。※毎年12月に翌々年分を追記すること（欠けても実害は
# 「祝日に平日どおり動く＝従来挙動」のみ。誤登録すると平日に1日沈黙するので公式暦から転記）。
JP_HOLIDAYS = frozenset({
    # 2026
    "2026-01-01", "2026-01-12", "2026-02-11", "2026-02-23", "2026-03-20",
    "2026-04-29", "2026-05-03", "2026-05-04", "2026-05-05", "2026-05-06",
    "2026-07-20", "2026-08-11", "2026-09-21", "2026-09-22", "2026-09-23",
    "2026-10-12", "2026-11-03", "2026-11-23",
    # 2027
    "2027-01-01", "2027-01-11", "2027-02-11", "2027-02-23", "2027-03-21",
    "2027-03-22", "2027-04-29", "2027-05-03", "2027-05-04", "2027-05-05",
    "2027-07-19", "2027-08-11", "2027-09-20", "2027-09-23",
    "2027-10-11", "2027-11-03", "2027-11-23",
})


def is_jp_workday(ts: float | None = None) -> bool:
    """JSTで土日でも祝日でもない日か（cron は平日刻みだが祝日を知らないため、スキル側で弾く用）。"""
    import datetime as _dt
    jst = _dt.datetime.fromtimestamp(ts if ts is not None else now_ts(),
                                     _dt.timezone(_dt.timedelta(hours=9)))
    return jst.weekday() < 5 and jst.strftime("%Y-%m-%d") not in JP_HOLIDAYS


def _path(name: str) -> Path:
    return STATE_DIR / name


def load_json(name: str, default=None):
    p = _path(name)
    if not p.exists():
        return default if default is not None else {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default if default is not None else {}


def save_json(name: str, data) -> None:
    # temp に書いて os.replace で原子的に置換（同一FS）。並行読み手の部分読み取り(JSONDecodeError→空default)を根絶。
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(name)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


APPROVALS_LOCK = "/tmp/chiaki_apply.lock"  # 裁定台帳の書き手全員が共有（apply-rulingのcron/listenerと同一）


def approvals_lock():
    """pending_approvals.json の読み書き排他（再設計R3）。書き手3系統（apply-ruling/intakeの
    retract/propose-to-approval）が別ロックで load→modify→save し、状態遷移が競合で消え得た
    （2026-07-14 レビュー確定バグ）。apply-ruling は cron の flock / listener の flock で
    実行全体がこのロック下＝他の書き手が短くこれを取れば全書き込みが直列化される。"""
    import fcntl
    from contextlib import contextmanager

    @contextmanager
    def _lock():
        with open(APPROVALS_LOCK, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    return _lock()


def append_jsonl(name: str, row: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_path(name), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(name: str) -> list[dict]:
    p = _path(name)
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue  # 部分書込みの破損1行でキュー全体を殺さない（load_jsonと同じ寛容方針）
    return out


def load_policy() -> dict:
    return load_json("policy.json", {
        "quality_nudges_require_approval": True,
        "stall_nudge_wording_require_approval": True,
        "notion_writes_require_approval": True,
    })


def record_finding(kind: str, payload: dict) -> None:
    """承認が要る判断候補を findings キューに積む（propose-to-approval が #8902 へ出す）。"""
    append_jsonl("findings.jsonl", {"ts": now_ts(), "kind": kind, "status": "new", **payload})


def load_tuning(skill: str, n: int = 6) -> list:
    """既存 tuning.json（旧 soft 学習の凍結データ）。新規調整は chiaki-intake が Rule Registry へ起票（自動蓄積は廃止）。
    指定 skill＋general の直近 n 件を返す。各生成（silence/pdca/propose）が文面に反映する。"""
    t = load_json("tuning.json", {})
    items = list(t.get(skill, [])) + list(t.get("general", []))
    return [d.get("directive", d) if isinstance(d, dict) else d for d in items][-n:]


_PUNCT_END = "。！？!?、，…「」『』（）()：:〜・"
_MODEL_TAG = re.compile(r"[（(][ 　]*(?:GPT|Haiku|Opus)(?:[ 　]*[0-9.]+)?(?:・代替)?[ 　]*[）)]")
_MODEL_TAG_TAIL = re.compile(rf"[ \t　]*{_MODEL_TAG.pattern}[ \t　]*$")
_URL_LINE = re.compile(r"^<?https?://")


def ensure_punct(text: str) -> str:
    """各文末に句読点「。」を確実に付ける（メンション/チャンネル単独行・コードブロック内・空行は除外）。"""
    out, in_code = [], False
    for ln in (text or "").split("\n"):
        s = ln.rstrip()
        t2 = s.strip()
        if t2.startswith("```"):
            in_code = not in_code
            out.append(s)
            continue
        if in_code or not t2:
            out.append(s)
            continue
        if (t2.startswith("<@") or t2.startswith("<!")) and t2.endswith(">") and " " not in t2:
            out.append(s)  # メンション/チャンネル単独行
            continue
        if "://" in t2:
            out.append(s)  # URL 行
            continue
        if set(t2) <= set("ー—–-＝=・　 "):
            out.append(s)  # 区切り行（ーーーーー等）
            continue
        if "：" in t2 and len(t2.split("：", 1)[0]) <= 8:
            out.append(s)  # 短いラベル行（提案：/対象：/検知：等）は文末扱いしない
            continue
        if s[-1] not in _PUNCT_END:
            s = s + "。"
        out.append(s)
    return "\n".join(out)


def _strip_model_tag_tail(text: str) -> str:
    """末尾の手書き/既存モデル表記を除く。本文中の通常の括弧表現は触らない。"""
    s = (text or "").rstrip()
    while True:
        ns = _MODEL_TAG_TAIL.sub("", s).rstrip()
        if ns == s:
            return s
        s = ns


def append_model_tag(text: str, tag: str) -> str:
    """LLM使用モデル表記を1つだけ、最後の本文行の文末へ続けて付ける。

    既に本文末に（GPT）等が含まれる場合は剥がしてから自動付与し直す。末尾がURL行なら
    URLを壊さないよう、その直前の本文行へ付ける。
    """
    tag = (tag or "").strip()
    if not tag:
        return text
    suffix = f"（{tag}）"
    lines = _strip_model_tag_tail(text or "").split("\n")
    target = None
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if not stripped or _URL_LINE.match(stripped):
            continue
        target = i
        break
    if target is None:
        base = "\n".join(lines).rstrip()
        return f"{base}{suffix}" if base else suffix
    line = _strip_model_tag_tail(lines[target])
    if line and line[-1] not in _PUNCT_END:
        line += "。"
    lines[target] = line + suffix
    return "\n".join(lines).rstrip()
