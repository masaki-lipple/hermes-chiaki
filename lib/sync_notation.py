#!/usr/bin/env python3
"""Notion の 用語辞書_DB ＋ レギュレーション_DB ＋ Style page → state/ に焼く（日本語ルール3層・§3.5）。

出力（1回の取得から3ファイル・atomic）:
  notation_rules.json  … 既存（obs-batch/notation_check が使用・後方互換のため変えない）
  regulations.json     … 決定論強制レイヤー（term_replacements/regex_rules/regulation_notes）
  style_hermes.md      … スタイル(prose)。lib/llm.py が生成 system に焼く。
「強制してよい線」は同期時に LLM(Haiku) が分類（blind 置換して安全か）。危険語 denylist＋単漢字は強制しない（本丸の安全網）。
box で daily 実行＋配備時に1回。NOTION_INTEGRATION_TOKEN / ANTHROPIC_API_KEY は profile .env か環境変数。標準ライブラリのみ。
"""
from __future__ import annotations
import datetime
import json
import os
import re
import urllib.request
from pathlib import Path

# database_id（既存・2022-06-28 でクエリ可。doc の data-source id と同一DBの別形式）
YOUGO_DB = "876b0c67-4a6f-4d09-ba83-6c9ca822c7a3"   # 用語辞書_DB（固有名詞・誤変換）
REG_DB = "2a1b88bf-9326-4ffc-aaf5-e6608871b5e0"     # レギュレーション_DB（誤例→正例）
STYLE_PAGE = "389980d4-f840-8133-8aa2-c66b857aa8ff"  # Style_Hermes Agent_総論（prose）
ACRONYMS = ["SNS", "EC", "SEO", "AI", "CV", "CVR", "KPI", "URL", "HP", "DM", "FAQ", "HR"]
# 同じ表記でも文脈で正しい用法があり blind 置換すると壊れる語＝Opus が安全と言っても強制しない安全網
RISKY_DENYLIST = {"等", "様", "時", "良い", "よい", "頂く", "いただく", "生かす", "活かす",
                  "二人", "2人", "物", "所", "方", "他", "為", "事", "中", "間", "上", "下"}
_SPLIT = re.compile(r"[、,／/]\s*")
JST = datetime.timezone(datetime.timedelta(hours=9))


def _profile_dir() -> Path:
    return Path(os.environ.get("HERMES_PROFILE_DIR")
                or Path.home() / ".hermes/profiles/management")


def _token() -> str:
    t = os.environ.get("NOTION_INTEGRATION_TOKEN")
    if t:
        return t
    env = _profile_dir() / ".env"
    if env.exists():
        for ln in env.read_text(encoding="utf-8").splitlines():
            if ln.startswith("NOTION_INTEGRATION_TOKEN="):
                return ln.split("=", 1)[1].strip()
    raise SystemExit("NOTION_INTEGRATION_TOKEN が見つかりません（環境変数 or profile .env）")


def _query(db_id: str, token: str) -> list[dict]:
    H = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28",
         "Content-Type": "application/json"}
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(f"https://api.notion.com/v1/databases/{db_id}/query",
                                     data=json.dumps(body).encode(), headers=H, method="POST")
        res = json.load(urllib.request.urlopen(req, timeout=30))
        rows += res.get("results", [])
        if not res.get("has_more"):
            break
        cursor = res.get("next_cursor")
    return rows


def _txt(props: dict, name: str) -> str:
    p = props.get(name, {})
    arr = p.get("title") or p.get("rich_text") or []
    return "".join(t.get("plain_text", "") for t in arr).strip()


def _sel(props: dict, name: str) -> str:
    return ((props.get(name, {}) or {}).get("select") or {}).get("name", "")


def _split(s: str) -> list[str]:
    return [x.strip() for x in _SPLIT.split(s) if x.strip()]


def _atomic_write(path: Path, text: str) -> None:
    """temp→os.replace で原子的に書く（読み取り中破損防止・失敗時は前回を保持）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ── 既存 notation_rules.json（後方互換・変えない） ──────────────
def build(token: str = None, yougo_rows: list = None, reg_rows: list = None) -> dict:
    if yougo_rows is None:
        yougo_rows = _query(YOUGO_DB, token)
    if reg_rows is None:
        reg_rows = _query(REG_DB, token)
    terms = []
    for pg in yougo_rows:
        p = pg["properties"]
        official = _txt(p, "正式表記")
        if not official:
            continue
        terms.append({
            "official": official,
            "aliases": _split(_txt(p, "別称")),
            "misconversions": _split(_txt(p, "誤変換パターン")),
            "category": _sel(p, "カテゴリ"),
            "minutes": _txt(p, "議事録表記"),
        })
    style_rules = []
    for pg in reg_rows:
        p = pg["properties"]
        rule = _txt(p, "ルール")
        wrongs = _split(_txt(p, "誤例"))
        rights = _split(_txt(p, "正例"))
        if not wrongs:
            continue
        for i, w in enumerate(wrongs):
            right = rights[i] if i < len(rights) else (rights[0] if rights else "")
            style_rules.append({"rule": rule, "wrong": w, "right": right})
    return {"_source": "Notion 用語辞書_DB + レギュレーション_DB (sync_notation.py)",
            "terms": terms, "acronyms": ACRONYMS, "style_rules": style_rules}


# ── 新 regulations.json（決定論強制レイヤー） ──────────────────
# builtin の構造ルール（行に割れる・一意。記事スコープに限定し chiaki 社内の全角！は保持）
_BUILTIN_REGEX = [
    {"id": "fullwidth-bang", "description": "全角！を半角！に", "pattern": "！", "replace": "!",
     "scope": ["記事・コンテンツ"], "kind": "Lipple"},
    {"id": "double-space", "description": "二重スペースを単一に", "pattern": "  +", "replace": " ",
     "scope": ["記事・コンテンツ"], "kind": "Lipple"},
]


# 分類は判断タスクだが、危険語 denylist＋単漢字ガードが本丸の安全網なので Haiku で十分（1日1回・低コスト）。
# 精度を上げたい時は llm.opus に差し替えるだけ。
_CLASSIFY_SYS = "あなたは正確な日本語校正・分類アシスタントです。指示に厳密に従い、指定のJSON以外は出力しない。"


def classify_rules(reg_rows: list, token: str = "") -> dict:
    """各レギュレーションを Haiku で『blind 置換して安全か』分類。{page_id: bool}。失敗時は空＝全 false（安全側）。"""
    items = []
    for pg in reg_rows:
        p = pg["properties"]
        if not _split(_txt(p, "誤例")):
            continue
        items.append({"id": pg["id"], "rule": _txt(p, "ルール"), "body": _txt(p, "ルール内容")[:300],
                      "wrong": _txt(p, "誤例"), "right": _txt(p, "正例")})
    if not items:
        return {}
    try:
        from lib import llm
    except Exception:
        try:
            import llm  # type: ignore
        except Exception:
            return {}
    prompt = (
        "次の日本語表記ルール群について、各ルールが『blind な文字列置換（誤例→正例を機械的に全置換）しても安全か』を判定。\n"
        "true=どんな文脈に出ても必ず正例に直すべき（例: 出来る→できる／宜しく→よろしく／子供→子ども／美味しい→おいしい）。\n"
        "false=同じ表記でも文脈で正しい用法があり全置換すると壊れる（例: 等→など だと「等しい」が壊れる／時→とき だと「時間」／"
        "様→さま だと「様子」「神様」／良い・頂く は品詞次第）。迷ったら false。\n"
        f"ルール(JSON配列): {json.dumps(items, ensure_ascii=False)}\n"
        'JSON のみで返す: {"<id>": true, "<id>": false, ...}'
    )
    try:
        out = llm.haiku(prompt, system=_CLASSIFY_SYS, max_tokens=2000) or ""
        m = re.search(r"\{.*\}", out, re.S)
        d = json.loads(m.group(0)) if m else {}
        return {k: bool(v) for k, v in d.items()}
    except Exception as e:
        print(f"[sync] classify_rules failed (全て notes 扱い): {e}")
        return {}


def build_regulations(yougo_rows: list, reg_rows: list, decidable: dict) -> dict:
    """用語＋レギュレーション → regulations.json。decidable[page_id]==True かつ非危険・非単漢字のみ regex 強制。"""
    term_replacements = []
    for pg in yougo_rows:
        p = pg["properties"]
        official = _txt(p, "正式表記")
        wrongs = [w for w in _split(_txt(p, "誤変換パターン")) if w and w != official]
        if not (official and wrongs):
            continue
        term_replacements.append({
            "correct": official, "wrong_patterns": wrongs,
            "category": _sel(p, "カテゴリ"),
            "kana_guard": all(len(w) == 1 for w in wrongs),
            "source_url": pg.get("url", ""),
        })
    regex_rules = [dict(r) for r in _BUILTIN_REGEX]
    notes = []
    for pg in reg_rows:
        p = pg["properties"]
        rule, body = _txt(p, "ルール"), _txt(p, "ルール内容")
        wrongs, rights = _split(_txt(p, "誤例")), _split(_txt(p, "正例"))
        kind = _sel(p, "種別") or "Lipple"
        sc = _sel(p, "適用シーン")
        scope = [sc] if sc else ["社内コミュニケーション", "記事・コンテンツ"]
        url = pg.get("url", "")
        # blind 置換できるのは Opus が安全と判定し、危険語でなく、単漢字でないものだけ
        risky = any((w in RISKY_DENYLIST) or (len(w) <= 1) for w in wrongs)
        if wrongs and decidable.get(pg["id"]) and not risky:
            for i, w in enumerate(wrongs):
                right = rights[i] if i < len(rights) else (rights[0] if rights else "")
                if not right or right == w:
                    continue
                regex_rules.append({
                    "id": f"regu-{pg['id'][:8]}-{i}", "description": rule,
                    "pattern": re.escape(w), "replace": right,
                    "scope": scope, "kind": kind, "source_url": url})
        elif wrongs or body:
            notes.append({
                "rule": rule, "body": body, "wrong": "/".join(wrongs), "right": "/".join(rights),
                "category": _sel(p, "カテゴリ"), "kind": kind, "scene": scope,
                "decidable": False, "source_url": url})
    return {"generated_at": datetime.datetime.now(JST).isoformat(timespec="seconds"),
            "term_replacements": term_replacements, "acronyms": ACRONYMS,
            "regex_rules": regex_rules, "regulation_notes": notes}


# ── Style page → markdown ──────────────────────────────────
def fetch_style_markdown(token: str, page_id: str) -> str:
    """ページの blocks を簡易 markdown 化（heading/paragraph/list/quote、1段ネストまで）。失敗/空は ""。"""
    H = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}

    def _children(bid: str) -> list:
        out, cursor = [], None
        while True:
            url = f"https://api.notion.com/v1/blocks/{bid}/children?page_size=100"
            if cursor:
                url += f"&start_cursor={cursor}"
            res = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=30))
            out += res.get("results", [])
            if not res.get("has_more"):
                break
            cursor = res.get("next_cursor")
        return out

    def _rich(bl: dict, t: str) -> str:
        return "".join(x.get("plain_text", "") for x in (bl.get(t, {}) or {}).get("rich_text", []))

    pref = {"heading_1": "# ", "heading_2": "## ", "heading_3": "### ",
            "bulleted_list_item": "- ", "numbered_list_item": "1. ", "quote": "> "}
    lines = []
    try:
        blocks = _children(page_id)
    except Exception as e:
        print(f"[sync] style fetch failed: {e}")
        return ""
    for b in blocks:
        t = b.get("type", "")
        if t in pref or t in ("paragraph", "callout"):
            lines.append((pref.get(t, "") + _rich(b, t)).rstrip())
        if b.get("has_children") and t in ("toggle", "bulleted_list_item", "numbered_list_item", "callout"):
            try:
                for c in _children(b["id"]):
                    ct = c.get("type", "")
                    lines.append("  " + pref.get(ct, "") + _rich(c, ct))
            except Exception:
                pass
    return "\n".join(lines).strip()


def main():
    token = _token()
    yougo_rows = _query(YOUGO_DB, token)
    reg_rows = _query(REG_DB, token)
    state = _profile_dir() / "state"

    # 1) 既存 notation_rules.json（変えない）
    rules = build(yougo_rows=yougo_rows, reg_rows=reg_rows)
    _atomic_write(state / "notation_rules.json", json.dumps(rules, ensure_ascii=False, indent=2))

    # 2) regulations.json（有効のみ・Haiku が強制可否を分類＋denylist で二重ガード）
    active = [r for r in reg_rows if _sel(r["properties"], "ステータス") == "有効"]
    decidable = classify_rules(active, token)
    reg = build_regulations(yougo_rows, active, decidable)
    _atomic_write(state / "regulations.json", json.dumps(reg, ensure_ascii=False, indent=2))

    # 3) style_hermes.md（取得できた時だけ上書き）
    style_md = fetch_style_markdown(token, STYLE_PAGE)
    if style_md:
        _atomic_write(state / "style_hermes.md", style_md)

    print(f"[sync] notation terms={len(rules['terms'])} style_rules={len(rules['style_rules'])} | "
          f"regulations term={len(reg['term_replacements'])} regex={len(reg['regex_rules'])} "
          f"notes={len(reg['regulation_notes'])} (active={len(active)} decidable={sum(1 for v in decidable.values() if v)}) | "
          f"style_md={'yes' if style_md else 'no'} -> {state}")


if __name__ == "__main__":
    main()
