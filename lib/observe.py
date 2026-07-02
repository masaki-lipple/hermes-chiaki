"""観測ルールの決定論コア（§3）。

すべて純関数。入力は slacklib の正規化メッセージ dict のリスト。LLM は呼ばない。
本番では skill の scripts/ がこれを import して使う。判断・文面が要る所だけ上位で LLM。
"""
from __future__ import annotations
import re
import statistics
import unicodedata

# ── 共通 ──────────────────────────────────────────────
_CHANNEL_TAG = re.compile(r"<!channel>|<!here>")


def _headline(text: str) -> str:
    """最初の意味ある行（<!channel> 等を除く）。"""
    for ln in text.splitlines():
        s = _CHANNEL_TAG.sub("", ln).strip()
        if s:
            return s
    return ""


def _split_label(core: str) -> tuple[str | None, str]:
    """'種別：案件' を分割。全角/半角コロン対応。種別が無ければ (None, core)。"""
    core = core.strip()
    for sep in ("：", ":"):
        if sep in core:
            kind, _, name = core.partition(sep)
            return kind.strip(), name.strip()
    return None, core


def _base_name(name: str) -> str:
    """案件名から連番（①②③/(1)/丸数字/末尾数字）を除いた基底名。相場集計のキー。"""
    name = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩]+", "", name)
    name = re.sub(r"[0-9０-９]+本目", "", name)
    name = re.sub(r"\s*[（(]?\d+[）)]?\s*$", "", name)
    return name.strip()


# ── §3.1 / §3.4 予定工数のパース ───────────────────────
_SCHED_HEADER = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日[（(].[）)]の予定です")
_TIME_BLOCK = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[-〜~–]\s*(\d{1,2}):(\d{2})\s+(.+?)\s*$")


def is_schedule_post(text: str) -> bool:
    return "の予定です" in text


def parse_schedule(text: str) -> dict | None:
    """朝のスケジュール報告 → {date, blocks:[{start,end,hours,task,kind,name}]}。"""
    if not is_schedule_post(text):
        return None
    hm = _SCHED_HEADER.search(text)
    date = None
    if hm:
        date = f"{int(hm.group(1)):04d}-{int(hm.group(2)):02d}-{int(hm.group(3)):02d}"
    blocks = []
    for ln in text.splitlines():
        m = _TIME_BLOCK.match(ln)
        if not m:
            continue
        sh, sm, eh, em, task = m.groups()
        hours = (int(eh) * 60 + int(em) - int(sh) * 60 - int(sm)) / 60.0
        if hours <= 0:
            continue
        kind, name = _split_label(task)
        blocks.append({
            "start": f"{int(sh):02d}:{sm}", "end": f"{int(eh):02d}:{em}",
            "hours": round(hours, 2), "task": task.strip(),
            "kind": kind, "name": name,
        })
    return {"date": date, "blocks": blocks, "planned_hours_total": round(sum(b["hours"] for b in blocks), 2)}


# ── §3.2 / §3.3 / §3.7 実測・予実・突合 ────────────────
_EOW = "本日の業務を終了します"
_START_RE = re.compile(r"^(?:業務を中断し、)?(.+?)を開始します")
_END_RE = re.compile(r"^(?:業務を中断し、)?(.+?)を終了します")
_BREAK_RE = re.compile(r"(休憩|業務を中断)")
_RESUME_RE = re.compile(r"(再開します)")


def classify_event(text: str) -> dict | None:
    """1メッセージのイベント種別を判定。
    返り値 type ∈ {start,end,break,resume,eow,schedule,note,other}。"""
    h = _headline(text)
    if is_schedule_post(text):
        return {"type": "schedule"}
    if h.startswith(_EOW):
        return {"type": "eow"}
    m = _END_RE.match(h)
    if m and m.group(1) not in ("本日の業務",):
        kind, name = _split_label(m.group(1))
        return {"type": "end", "core": m.group(1).strip(), "kind": kind, "name": name}
    m = _START_RE.match(h)
    if m:
        kind, name = _split_label(m.group(1))
        return {"type": "start", "core": m.group(1).strip(), "kind": kind, "name": name}
    if _RESUME_RE.search(h):
        return {"type": "resume"}
    if _BREAK_RE.search(h):
        return {"type": "break"}
    if h.startswith("本日のノート"):
        return {"type": "note"}
    return {"type": "other"}


def extract_task_events(messages: list[dict]) -> dict:
    """開始↔終了を突合し実測工数を出す。
    2段階: ①core 完全一致（exact）→ ②残りを base_name で FIFO（fuzzy・低信頼）。
    返り値: {actuals:[...{match:'exact'|'fuzzy'}], unmatched:[...]}。
    §3.7 = unmatched が残ったら上位で1回だけ確認する一次シグナル。"""
    starts, ends = [], []
    for m in sorted(messages, key=lambda x: x["ts_float"]):
        ev = classify_event(m["text"])
        if not ev:
            continue
        rec = {**ev, "ts": m["ts_float"], "datetime": m["datetime"]}
        if ev["type"] == "start":
            starts.append(rec)
        elif ev["type"] == "end":
            ends.append(rec)

    actuals = []
    used_starts = set()

    def _emit(st, en, match):
        actuals.append({
            "core": en["core"], "kind": en.get("kind") or st.get("kind"),
            "name": en["name"], "base_name": _base_name(en["name"]),
            "start_ts": st["ts"], "end_ts": en["ts"],
            "actual_hours": round((en["ts"] - st["ts"]) / 3600.0, 2),
            "start_dt": st["datetime"], "end_dt": en["datetime"], "match": match,
        })

    leftover_ends = []
    for en in ends:
        # ① 完全一致: 同 core の最も新しい未使用 start で en より前
        cand = [i for i, st in enumerate(starts)
                if i not in used_starts and st["core"] == en["core"] and st["ts"] <= en["ts"]]
        if cand:
            i = cand[-1]
            used_starts.add(i)
            _emit(starts[i], en, "exact")
        else:
            leftover_ends.append(en)
    # ② fuzzy: base_name 一致の最も古い未使用 start（FIFO）で en より前
    for en in leftover_ends:
        b = _base_name(en["name"])
        cand = [i for i, st in enumerate(starts)
                if i not in used_starts and _base_name(st["name"]) == b and st["ts"] <= en["ts"]]
        if cand:
            i = cand[0]
            used_starts.add(i)
            _emit(starts[i], en, "fuzzy")
        else:
            pass  # 真に突合不能な end は捨てる（開始宣言なしの終了）

    unmatched = []
    for en in leftover_ends:
        b = _base_name(en["name"])
        if not any(_base_name(starts[i]["name"]) == b for i in used_starts):
            unmatched.append({"reason": "end_without_start", "core": en["core"], "datetime": en["datetime"]})
    for i, st in enumerate(starts):
        if i not in used_starts:
            unmatched.append({"reason": "start_without_end", "core": st["core"], "datetime": st["datetime"]})
    return {"actuals": sorted(actuals, key=lambda a: a["start_ts"]), "unmatched": unmatched}


# ── 種別の細分化（「種別（対象）」表記・全角かっこ・2026-07-03 戸田決定） ──
_PROG_RE = re.compile(r"^(?:業務を中断し、)?(.+?)を進めています")
_KIND2_FIX = (
    ("文言", ("タイトル", "文言", "誤字", "見出し")),
    ("固定ページ", ("固定ページ", "店舗一覧", "店舗固定", "トップページ")),
    ("求人ページ", ("求人",)),
    ("コンテンツページ", ("コンテンツ", "記事", "インタビュー", "コラム", "吹き出し", "赤文字")),
)
_KIND2_NAGASHI = (
    ("求人ページ", ("求人",)),
    ("商品ページ", ("商品",)),
    ("コンテンツ", ("コンテンツ", "記事", "インタビュー", "コラム", "原稿", "座談会")),
)


def _kind2_pick(rules: tuple, texts: list) -> str | None:
    for label, kws in rules:
        if any(kw in t for t in texts for kw in kws):
            return label
    return None


def refine_actual_kinds(actuals: list[dict], messages: list[dict]) -> None:
    """actuals へ kind2=「種別（対象）」（例: 修正（求人ページ）・全角かっこ）を in-place 付与
    （対象を特定できなければ付けない）。
    証拠の優先順: 修正=①core自身 ②同案件の開始/終了/進捗投稿 ③開始前の指示（type=other・案件名含む）
    ／流し込み=①core ③指示 ②自投稿。流し込みの開始/終了投稿は全件が
    「コンテンツの流し込みを行います」の定型文で対象を判別できないため、指示を先に見る。
    メモ・雑談（type=other で案件名なし）は証拠に使わない＝週次タスクメモ等の誤マッチ防止。
    指示は案件名と同じ節（、。！？改行区切り）だけを証拠にする＝「A が終わったら B の求人追加を」
    のような別案件への依頼に釣られない。"""
    evs = []
    for m in sorted(messages, key=lambda x: x["ts_float"]):
        ev = classify_event(m["text"]) or {}
        typ, core = ev.get("type"), ev.get("core")
        if typ == "other":
            pm = _PROG_RE.match(_headline(m["text"]))
            if pm:
                typ, core = "progress", pm.group(1).strip()
        evs.append((typ, core or "", m["text"], m["ts_float"]))

    for a in actuals:
        core = a.get("core") or ""
        if "修正" in core:
            rules, base, order = _KIND2_FIX, "修正", ("core", "own", "instr")
        elif "流し込み" in core:
            rules, base, order = _KIND2_NAGASHI, "流し込み", ("core", "instr", "own")
        else:
            continue
        name = a.get("base_name") or ""
        own = [t for typ, c, t, ts in evs
               if typ in ("start", "end", "progress")
               and (c == core or (len(name) >= 3 and name in c))
               and a["start_ts"] - 60 <= ts <= a["end_ts"] + 60]
        instr = [cl for typ, c, t, ts in evs
                 if typ == "other" and len(name) >= 3 and name in t
                 and a["start_ts"] - 86400 <= ts <= a["start_ts"] + 300
                 for cl in re.split(r"[、。！？!?\n]", t) if name in cl]
        texts = {"core": [core], "own": own, "instr": instr}
        for tier in order:
            label = _kind2_pick(rules, texts[tier])
            if label:
                a["kind2"] = f"{base}（{label}）"
                break


def reconcile_with_plan(plan_blocks: list[dict], actuals: list[dict]) -> list[dict]:
    """§3.3 予定 vs 実測。案件基底名で突合し差分を出す。突合不能は plan/actual のみ。"""
    out = []
    plan_by_base = {}
    for b in plan_blocks:
        plan_by_base.setdefault(_base_name(b["name"]), 0.0)
        plan_by_base[_base_name(b["name"])] += b["hours"]
    seen = set()
    for a in actuals:
        base = a["base_name"]
        planned = plan_by_base.get(base)
        out.append({"base_name": base, "kind": a["kind"], "planned_hours": planned,
                    "actual_hours": a["actual_hours"],
                    "delta": None if planned is None else round(a["actual_hours"] - planned, 2),
                    "matched": planned is not None})
        seen.add(base)
    for base, ph in plan_by_base.items():
        if base not in seen:
            out.append({"base_name": base, "kind": None, "planned_hours": ph,
                        "actual_hours": None, "delta": None, "matched": False,
                        "note": "予定にあるが実測なし"})
    return out


# ── §3.5 報告品質（表記・頭字語）— 候補抽出のみ（採否/文面は承認系） ──
_LATIN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9]*")


def _is_kana(ch: str) -> bool:
    return bool(ch) and ("ぁ" <= ch <= "ヿ")  # ひらがな〜カタカナ（長音ー含む）


def _is_kanji(ch: str) -> bool:
    return bool(ch) and ("一" <= ch <= "鿿")


# URL/Slackリンク<...>/コード`...` は本文(プローズ)ではない＝表記検査・置換の対象にしない。
# 例: campaign-url-builder のパス内 "url" を頭字語 URL と誤検知しない（戸田さん指摘 2026-06-30）。
# 素URLは ASCII の URL 文字のみにマッチ（`\S+` だと日本語で URL 直後に空白なく全角句点+本文が続く
# 「…com。snsで…」で後続プローズまで食い、表記検知を落とす＝敵対検証 high）。日本語/全角/空白で止まる。
_NONCONTENT = re.compile(r"<[^>\n]+>|https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+|`[^`\n]+`")


def _mask_noncontent(text: str) -> str:
    """URL/リンク/コードを等長の \\x00 で潰す（検出走査用・文字位置を保持／空白でないので二重空白等にも誤マッチしない）。"""
    return _NONCONTENT.sub(lambda mm: "\x00" * len(mm.group(0)), text)


def _protect_noncontent(text: str, transform):
    """URL/リンク/コードを単一の \\x00 に退避して transform(masked) を適用→出現順に復元（URL内を壊さない置換用）。
    プレースホルダに数字を使わない＝『数字を置換するルール』等でトークンが壊れて NUL が漏れることを防ぐ（敵対検証 medium）。"""
    return _protect(_NONCONTENT, text, transform)


# enforce（chiaki 自身の生成文の自動補正）では URL/リンク/コードに加えて「」引用と```フェンスも保護する。
# 指摘文・起票確認は誤例をわざと引用する（『「url」を「URL」に直して』等）＝引用内まで直すと
# 『「URL」を「URL」に直して』となり文意が壊れる（監査確定 high）。検知(report)側は従来どおり引用内も拾う。
_ENFORCE_MASK = re.compile(r"```[\s\S]*?```|「[^「」\n]{1,60}」|" + _NONCONTENT.pattern)


def _protect_enforce(text: str, transform):
    return _protect(_ENFORCE_MASK, text, transform)


def _protect(mask: re.Pattern, text: str, transform):
    toks = []

    def _stash(mm):
        toks.append(mm.group(0))
        return "\x00"
    out = transform(mask.sub(_stash, text))
    for t in toks:
        out = out.replace("\x00", t, 1)
    return out


def notation_check(text: str, rules: dict) -> list[dict]:
    """高確度の表記候補を抽出。種別ズレ等の判断は上位 LLM に委ねる。"""
    issues = []
    scan = _mask_noncontent(text)  # URL/リンク/コード内の語は表記検査しない（campaign-url-builder の url 等）
    # 頭字語の casing: 小文字トークンが既知頭字語と一致 → 大文字を提案
    acro = {a.upper() for a in rules.get("acronyms", [])}
    for m in _LATIN_RUN.finditer(scan):
        tok = m.group(0)
        if tok.upper() in acro and tok != tok.upper() and tok.lower() == tok:
            issues.append({"kind": "acronym_casing", "found": tok,
                           "suggest": tok.upper(), "confidence": "high"})
    # 誤変換パターン（用語辞書_DB）: 本文に出現 → 正式表記を提案
    for term in rules.get("terms", []):
        for mis in term.get("misconversions", []):
            if mis and mis in scan:
                issues.append({"kind": "misconversion", "found": mis,
                               "suggest": term["official"], "confidence": "high"})
    # スタイルルール（レギュレーション_DB の 誤例→正例）: 誤例が出現 → 正例を提案
    for r in rules.get("style_rules", []):
        w, right = r.get("wrong"), r.get("right")
        if not (w and w in scan):
            continue
        # 1文字の漢字ルール（事/為/等＝形式名詞・連用）は複合語(記事/行為/均等)に誤マッチしやすい。
        # 直前が仮名のとき＝形式名詞・連用用法のときだけ拾う（複合語は直前が漢字なので除外）。
        if len(w) == 1 and _is_kanji(w):
            if not any(i > 0 and _is_kana(scan[i - 1]) for i, ch in enumerate(scan) if ch == w):
                continue
        issues.append({"kind": "style_rule", "found": w, "suggest": right,
                       "rule": r.get("rule"), "confidence": "high"})
    # 重複除去
    uniq = {(i["kind"], i["found"], i["suggest"]): i for i in issues}
    return list(uniq.values())


def apply_notation_fixes(text: str, rules: dict) -> tuple[str, list]:
    """chiaki 自身の生成文を規約に通して自動補正（誤例→正例・誤変換→正式・頭字語casing）。
    高確度のもののみ単純置換。返り値: (補正後テキスト, 適用リスト)。"""
    subs = [(i["found"], i["suggest"]) for i in notation_check(text, rules)
            if i.get("found") and i.get("suggest") and i["found"] != i["suggest"]]
    applied = []

    def _apply(s):
        for f, sg in subs:
            if f in s:
                s = s.replace(f, sg)
                applied.append((f, sg))
        return s
    fixed = _protect_enforce(text, _apply)  # URL/リンク/コード/「」引用/```フェンス内は壊さない
    return fixed, applied


# ── 日本語ルール3層（regulations.json の決定論レイヤー） ─────────────
def _kana_preceded(text: str, ch: str) -> bool:
    """text 中の ch のいずれかの出現が、直前に仮名を持つ（＝形式名詞・連用用法）か。"""
    return any(i > 0 and _is_kana(text[i - 1]) for i, c in enumerate(text) if c == ch)


def apply_regulations(text: str, reg: dict, scene: str = "社内コミュニケーション",
                      mode: str = "report"):
    """regulations.json の決定論ルールのみ適用（LLM 不使用）。
    mode='report'  → (text, findings)      findings は notation_check 互換 dict
    mode='enforce' → (fixed_text, applied)  applied は [(found, suggest, kind)]
    単漢字は仮名ガード／regex は scene で絞る／正式表記との同一は置換しない。"""
    findings = []
    scan = _mask_noncontent(text)  # URL/リンク/コード内の語は表記検査・置換しない（campaign-url-builder の url 等）
    # 1) 頭字語 casing（sns → SNS 等）
    acro = {a.upper() for a in reg.get("acronyms", [])}
    for m in _LATIN_RUN.finditer(scan):
        tok = m.group(0)
        if tok.upper() in acro and tok == tok.lower() and tok != tok.upper():
            findings.append({"kind": "acronym_casing", "found": tok,
                             "suggest": tok.upper(), "confidence": "high"})
    # 2) 用語の誤変換（誤 → 正式表記）
    for t in reg.get("term_replacements", []):
        correct = t.get("correct", "")
        for w in t.get("wrong_patterns", []):
            if not (w and w in scan and w != correct):
                continue
            if (t.get("kana_guard") or (len(w) == 1 and _is_kanji(w))) and not _kana_preceded(scan, w):
                continue
            findings.append({"kind": "misconversion", "found": w,
                             "suggest": correct, "confidence": "high"})
    # 3) regex ルール（scene フィルタ・decidable のみ同期済み）
    rx = []
    for r in reg.get("regex_rules", []):
        if scene not in (r.get("scope") or [scene]):
            continue
        pat, rep = r.get("pattern", ""), r.get("replace", "")
        if pat and re.search(pat, scan):
            rx.append((pat, rep, r.get("id") or pat))
            findings.append({"kind": "regex", "found": r.get("id") or pat,
                             "suggest": rep, "rule": r.get("description"), "confidence": "high"})
    uniq = {(f["kind"], f["found"], f["suggest"]): f for f in findings}
    findings = list(uniq.values())
    if mode != "enforce":
        return text, findings
    applied = []

    def _content_fixes(s):
        # URL/リンク/コード/「」引用/```フェンスは _protect_enforce で退避済み（\x00 1文字）＝中を壊さない。
        for f in findings:
            if f["kind"] in ("acronym_casing", "misconversion") and f["found"] in s and f["found"] != f["suggest"]:
                ns = s.replace(f["found"], f["suggest"])
                if ns != s:
                    s = ns
                    applied.append((f["found"], f["suggest"], f["kind"]))
        for pat, rep, rid in rx:
            ns = re.sub(pat, rep, s)
            if ns != s:
                s = ns
                applied.append((rid, rep, "regex"))
        # builtin: 全角英数字 → 半角（数字も英語も半角・戸田レギュレーション）。全角「！」等の記号は
        # 変換しない＝トーン保持。引用・フェンス内の教示例（「１２３」等）は退避済みで対象外。
        _fw = "".join(chr(c) for c in [*range(0xFF10, 0xFF1A), *range(0xFF21, 0xFF3B), *range(0xFF41, 0xFF5B)])
        if any(z in s for z in _fw):
            _hw = "".join(chr(c) for c in [*range(0x30, 0x3A), *range(0x41, 0x5B), *range(0x61, 0x7B)])
            ns = s.translate(str.maketrans(_fw, _hw))
            if ns != s:
                s = ns
                applied.append(("全角英数字", "半角", "fullwidth_alnum"))
        # builtin: 自己言及は「Chiaki AI」に統一（小文字 chiaki のプローズ表記・戸田レギュレーション）。
        # ラテン文字/ハイフン/アンダースコアに挟まれた chiaki（chiaki-intake 等の識別子）は対象外＝安全。
        if "chiaki" in s:
            ns = re.sub(r"(?<![A-Za-z0-9_-])chiaki(?![A-Za-z0-9_-])", "Chiaki AI", s)
            if ns != s:
                s = ns
                applied.append(("chiaki", "Chiaki AI", "self_reference"))
        # builtin: 英数字↔日本語(かな/漢字)の境目の半角スペースを削除（戸田レギュレーション）。
        # 固有名詞内の空白（Claude Code 等＝両側ラテン）は境目でないので残る。
        # 退避トークン \x00 は英数字でも日本語でもない＝URL/リンク越しに接着しない。
        _JP = r"[぀-ヿ㐀-鿿々〆]"
        ns = re.sub(rf"([A-Za-z0-9]) +({_JP})", r"\1\2", s)
        ns = re.sub(rf"({_JP}) +([A-Za-z0-9])", r"\1\2", ns)
        if ns != s:
            s = ns
            applied.append(("境界半角スペース", "削除", "boundary_space"))
        return s
    fixed = _protect_enforce(text, _content_fixes)
    # builtin: 行頭の「・」箇条書きは Slack のリスト記法「• 」に統一（戸田レギュレーション。
    # 行中の「・」は区切り（GO・却下 等）なので不変。「・・・」等の連続は対象外）。
    nb_b = re.sub(r"(?m)^([ 　]*)・(?!・)[ 　]?", r"\1• ", fixed)
    if nb_b != fixed:
        fixed = nb_b
        applied.append(("行頭・", "• ", "bullet"))
    # builtin: 行頭が URL だけの行は、直前が本文なら空行を1つ入れる（戸田レイアウト）。
    _lines, _out = fixed.split("\n"), []
    for _l in _lines:
        if re.match(r"<?https?://", _l.strip()) and _out and _out[-1].strip() != "":
            _out.append("")
        _out.append(_l)
    nb2 = "\n".join(_out)
    if nb2 != fixed:
        fixed = nb2
        applied.append(("URL前空行", "挿入", "url_blank_line"))
    return fixed, applied


def load_regulations() -> dict:
    """regulations.json をロード（HERMES_REGULATIONS env → profile state → repo fixtures → 空）。"""
    import json as _json
    import os as _os
    from pathlib import Path as _Path
    cands = []
    if _os.environ.get("HERMES_REGULATIONS"):
        cands.append(_Path(_os.environ["HERMES_REGULATIONS"]))
    if _os.environ.get("HERMES_PROFILE_DIR"):
        cands.append(_Path(_os.environ["HERMES_PROFILE_DIR"]) / "state" / "regulations.json")
    root = _Path(__file__).resolve().parents[1]
    cands.append(root / "profile" / "state" / "regulations.json")
    cands.append(root / "fixtures" / "notion" / "regulations.json")
    for c in cands:
        try:
            if c.exists():
                return _json.loads(c.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"term_replacements": [], "regex_rules": [], "regulation_notes": [], "acronyms": []}


def enforce_regulations(text: str, scene: str = "社内コミュニケーション") -> str:
    """chiaki 自身の出力を投稿直前に決定論で整える（失敗時は原文・LLM不使用）。"""
    try:
        fixed, _ = apply_regulations(text, load_regulations(), scene=scene, mode="enforce")
        return fixed
    except Exception:
        return text


# ── §3.6 65分無音リマインド ─────────────────────────────
SILENCE_THRESHOLD_SEC = 65 * 60


def silence_decision(messages: list[dict], now_ts: float,
                     threshold_sec: int = SILENCE_THRESHOLD_SEC,
                     already_reminded_after_ts: float | None = None,
                     owner_id: str | None = None) -> dict:
    """今 now_ts 時点で鳴らすべきか。終業後は鳴らさない・連打しない。
    owner_id 指定時は『対象者(報告すべき松永さん)自身の投稿』だけで判定する＝管理者/戸田さんの投稿を
    1時間ルールの基準にしない（他者の投稿に『報告して』と誤爆しない・対象者が未投稿の日は鳴らさない・
    終業も対象者基準で見る）。指定なしは従来どおり全投稿で判定。"""
    if not messages:
        return {"fire": False, "reason": "no_messages"}
    msgs = sorted(messages, key=lambda x: x["ts_float"])
    if owner_id is not None:
        msgs = [m for m in msgs if m.get("user_id") == owner_id]
        if not msgs:
            return {"fire": False, "reason": "owner_no_posts"}
    last = msgs[-1]
    # 終業が（対象者の）当日投稿に出ていれば打ち止め（終業後にノート画像等が続くため最後だけ見ない）
    if any(classify_event(m["text"]).get("type") == "eow" for m in msgs):
        return {"fire": False, "reason": "eow_reached", "last_ts": last["ts_float"]}
    gap = now_ts - last["ts_float"]
    if gap < threshold_sec:
        return {"fire": False, "reason": "within_threshold", "gap_min": round(gap / 60, 1)}
    if already_reminded_after_ts is not None and already_reminded_after_ts >= last["ts_float"]:
        return {"fire": False, "reason": "already_reminded", "gap_min": round(gap / 60, 1)}
    return {"fire": True, "reason": "silent_over_threshold", "gap_min": round(gap / 60, 1),
            "target_ts": last["ts"], "target_dt": last["datetime"]}


def silence_dry_run(messages: list[dict], threshold_sec: int = SILENCE_THRESHOLD_SEC) -> list[dict]:
    """履歴上で「どこで鳴るはずだったか」を検出（昼休憩はすり抜ける想定）。連投間ギャップで判定。"""
    msgs = sorted(messages, key=lambda x: x["ts_float"])
    fires = []
    for i in range(len(msgs) - 1):
        a, b = msgs[i], msgs[i + 1]
        if classify_event(a["text"]).get("type") == "eow":
            continue  # 終業後は対象外（同日内）
        gap = b["ts_float"] - a["ts_float"]
        if gap >= threshold_sec:
            fires.append({"after_dt": a["datetime"], "next_dt": b["datetime"],
                          "gap_min": round(gap / 60, 1),
                          "after_headline": _headline(a["text"])[:40]})
    return fires


# ── §3.8 タスク軸の停滞検知（業務チャンネル）──────────────
_BIZ_TASK = re.compile(r"業務内容[：:]\s*(.+)")
_BIZ_DUE = re.compile(r"対応期限[：:]\s*(\d{4})年(\d{1,2})月(\d{1,2})日")
# 対応者：<@U…>（複数可・空欄あり）。促し(Phase2)の宛先の正＝タスク投稿自身のフィールド。
_BIZ_ASSIGNEE = re.compile(r"対応者[：:]\s*((?:\s*<@[A-Z0-9]+>)*)")
_MENTION_ID = re.compile(r"<@([A-Z0-9]+)>")


def parse_biz_task(text: str) -> dict | None:
    tm = _BIZ_TASK.search(text)
    if not tm:
        return None
    due = None
    dm = _BIZ_DUE.search(text)
    if dm:
        due = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
    assignees = []
    am = _BIZ_ASSIGNEE.search(text)
    if am:
        assignees = _MENTION_ID.findall(am.group(1))
    return {"task": tm.group(1).strip(), "due": due, "assignees": assignees}


# GCP タスク同期 bot。§3.8: この bot 自身の投稿は「動き」に数えない（活動＝人間の反応のみ）。
GCP_TASK_BOT = "U0BBZ3B3UNS"


def human_activity_count(root_msg: dict, bot_user_ids: set[str]) -> int:
    """そのタスク根スレッドの『人間の活動』数。
    本番(box)はスレッドを読み bot 著者を除いた返信数を root_msg['human_replies'] に入れて渡す。
    無ければ thread_replies で代用（bot 単独返信を分離できない＝過小に活動扱いするリスクあり、と記録）。"""
    if "human_replies" in root_msg and root_msg["human_replies"] is not None:
        return int(root_msg["human_replies"])
    return int(root_msg.get("thread_replies") or 0)


def stall_scan(messages: list[dict], now_ts: float,
               no_pickup_days: int = 3, deadline_window_days: int = 2,
               open_window_days: int = 30, expired_grace_days: int = 7,
               bot_user_ids: set[str] | None = None) -> list[dict]:
    """タスクスレッド根が生きているか。着手なし / 期限近接で無動 を拾う（検知=決定論）。
    §3.8: 活動は『人間の反応』のみで数える（GCP bot の投下は動きにしない）。
    対象は「まだ開いていそうな」根だけ（期限が未来、または投稿が直近 open_window_days 以内）。
    期限を expired_grace_days 以上過ぎた古い根は対象外（完了/放置とみなす）。"""
    import datetime as _dt
    JST = _dt.timezone(_dt.timedelta(hours=9))
    DAY = 86400
    bots = bot_user_ids or {GCP_TASK_BOT}
    out = []
    for m in messages:
        t = parse_biz_task(m["text"])
        if not t:
            continue
        human_replies = human_activity_count(m, bots)
        age_days = (now_ts - m["ts_float"]) / DAY
        days_to_due = None
        if t["due"]:
            try:
                due_ts = _dt.datetime.fromisoformat(t["due"]).replace(tzinfo=JST).timestamp()
                days_to_due = (due_ts - now_ts) / DAY
            except ValueError:
                pass
        # スコープ: 期限切れ(>grace)の古い根は除外。開いていそうな根だけ見る。
        if days_to_due is not None and days_to_due < -expired_grace_days:
            continue
        is_open_ish = (days_to_due is not None and days_to_due >= -expired_grace_days) or age_days <= open_window_days
        if not is_open_ish:
            continue
        signals = []
        if human_replies == 0 and no_pickup_days <= age_days:
            signals.append("no_pickup")
        if days_to_due is not None and -1 <= days_to_due <= deadline_window_days and human_replies == 0:
            signals.append("deadline_no_movement")
        if signals:
            out.append({"task": t["task"][:50], "due": t["due"],
                        "human_replies": human_replies, "thread_replies": m.get("thread_replies"),
                        "root_by_bot": m.get("user_id") in bots,
                        "age_days": round(age_days, 1),
                        "days_to_due": None if days_to_due is None else round(days_to_due, 1),
                        "posted": m["datetime"], "signals": signals})
    return out


# ── §3.3 種別×案件 の相場 ──────────────────────────────
def compute_baselines(actuals: list[dict]) -> dict:
    """(種別 × 案件基底) と 種別単体 の実測統計。"""
    def agg(rows):
        hrs = [r["actual_hours"] for r in rows if r["actual_hours"] is not None]
        if not hrs:
            return None
        return {"n": len(hrs), "mean_h": round(statistics.mean(hrs), 2),
                "median_h": round(statistics.median(hrs), 2),
                "min_h": round(min(hrs), 2), "max_h": round(max(hrs), 2)}

    by_pair: dict[tuple, list] = {}
    by_kind: dict[str, list] = {}
    for a in actuals:
        kind = a.get("kind2") or a.get("kind") or "?"
        by_pair.setdefault((kind, a["base_name"]), []).append(a)
        by_kind.setdefault(kind, []).append(a)
    return {
        "by_kind_x_case": {f"{k0}×{k1}": agg(v) for (k0, k1), v in sorted(by_pair.items())},
        "by_kind": {k: agg(v) for k, v in sorted(by_kind.items())},
    }
