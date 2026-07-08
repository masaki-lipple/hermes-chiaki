#!/usr/bin/env python3
"""chiaki-pdca（§6 自己PDCA→#5902／決定論digest＋Haiku3行・@channel・会話エージェント不使用）。

Lipple の1時間ルールPDCAに準拠（#5029-ayaka-pdca を手本）:
  9:00 計画 / 10:00-17:00 毎時 進捗 / 18:00 終了報告。各 top-level＋@channel(<!channel>)・3行。
モードは現在時刻(JST)で判定（<10=計画 / 10-17=進捗 / >=18=終了）。スロット単位で重複ガード。
進捗は「前回投稿(last_post_ts)からの差分」を数字化し Haiku が3行に整える。
cron: 0 9 * * 1-5 / 0 10-17 * * 1-5 / 0 18 * * 1-5。
"""
import datetime as dt
import os
import re
import sys
from collections import Counter
from pathlib import Path
sys.path.insert(0, os.environ.get("HERMES_LIB") or str(Path(__file__).resolve().parents[5]))
from lib import observe, runtime, source  # noqa: E402

JST = dt.timezone(dt.timedelta(hours=9))


def _now():
    return dt.datetime.now(JST)


def _tsd(ts) -> str:
    try:
        return dt.datetime.fromtimestamp(float(ts), JST).strftime("%Y-%m-%d")
    except Exception:
        return ""


def _mode(hour: int) -> str:
    if hour < 10:
        return "morning"
    if hour < 18:
        return "progress"
    return "close"


def _day_findings(date):
    return [f for f in runtime.read_jsonl("findings.jsonl") if _tsd(f.get("ts")) == date]


def _day_rulings(date):
    return [r for r in runtime.read_jsonl("rulings.jsonl") if _tsd(r.get("ts")) == date]


def _progress_digest(since: float, date: str) -> dict:
    bots = {runtime.GCP_TASK_BOT, runtime.CHIAKI_SELF}
    recent = source.read_recent(runtime.CH_YU_PDCA, oldest_ts=since or None, limit=200)
    reports = sum(1 for m in recent if m["ts_float"] > (since or 0)
                  and m["user_id"] not in bots and m["datetime"][:10] == date)
    fnew = [f for f in _day_findings(date) if float(f.get("ts", 0)) > (since or 0)]
    rnew = [r for r in _day_rulings(date) if float(r.get("ts", 0)) > (since or 0)]
    fk, rv = Counter(f.get("kind") for f in fnew), Counter(r.get("verdict") for r in rnew)
    return {"reports": reports, "notation": fk.get("notation", 0), "typo": fk.get("typo", 0),
            "stall": fk.get("stall", 0), "go": rv.get("go", 0) + rv.get("interpret", 0),
            "reject": rv.get("reject", 0), "completed": rv.get("completed", 0)}


def _close_digest(date: str) -> dict:
    actuals = runtime.load_json(f"actuals_{date}.json", {})
    fk = Counter(f.get("kind") for f in _day_findings(date))
    rv = Counter(r.get("verdict") for r in _day_rulings(date))
    pend = runtime.load_json("pending_approvals.json", {"items": {}}).get("items", {})
    return {"actuals": len(actuals.get("actuals", [])), "unmatched": len(actuals.get("unmatched", [])),
            "notation": fk.get("notation", 0), "typo": fk.get("typo", 0), "stall": fk.get("stall", 0),
            "go": rv.get("go", 0) + rv.get("interpret", 0), "reject": rv.get("reject", 0),
            "completed": rv.get("completed", 0),
            "open": sum(1 for it in pend.values() if it.get("status") in ("pending", "awaiting_completion"))}


# 始業報告に列挙しない＝chiaki 自身の発信ch（#5902/#8902）。それ以外の参加chは観測対象として列挙。
# typo-scan の _EXCLUDE と揃える＝「監視しているch」と「朝に列挙するch」を一致させる。
_OBSERVE_EXCLUDE = {runtime.CH_CHIAKI_PDCA, runtime.CH_CHIAKI_MGMT}


def _observed_channels() -> list[str]:
    """観測対象＝bot が参加する業務チャンネル全部（新しく招待されたchも自動で含む）。
    取得失敗時は最低限 #5035 だけ返す（朝の報告を空にしない）。"""
    try:
        chs = [c["id"] for c in source.list_bot_channels()
               if c.get("id") and c["id"] not in _OBSERVE_EXCLUDE]
        return chs or [runtime.CH_YU_PDCA]
    except Exception:
        return [runtime.CH_YU_PDCA]


def _morning_text() -> str:
    """始業のあいさつ（戸田テンプレ・観測チャンネルをリンクで列挙・決定論／終業・毎時とは別形式）。"""
    obs = "\n".join(f"<#{c}>" for c in _observed_channels())
    return ("おはようございます。\n"
            "本日の観測を開始します。\n\n"
            "いくつかのチャンネルを観測し、1時間ルール・表記誤字・予実・停滞を見ます。\n\n"
            "本日観測するチャンネルはこちらです。\n\n"
            f"{obs}\n\n"
            f"気づきや提案は <#{runtime.CH_CHIAKI_MGMT}> に上げ、完了確認とリマインドも行います。\n"
            "本日もよろしくおねがいします！")


def _fallback_lines(mode: str, d: dict) -> str:
    """Haiku 不通（残高切れ・障害）でも PDCA を止めない決定論3行（数字は digest から機械的に）。
    2026-06-29 の残高切れで毎時報告が無言停止した反省＝生成が死んでも固定文で継続する。"""
    if mode == "progress":
        det = f"表記{d['notation']}・誤字{d['typo']}・停滞{d['stall']}、反映{d['go']}・却下{d['reject']}・修正完了{d['completed']}です。"
        return (f"松永さんからの報告{d['reports']}件を確認しました。\n{det}\n引き続き、観測を続けます。")
    det = (f"実測{d['actuals']}件・未突合{d['unmatched']}件、表記{d['notation']}・誤字{d['typo']}・停滞{d['stall']}、"
           f"反映{d['go']}・却下{d['reject']}・修正完了{d['completed']}・未対応{d['open']}件です。")
    return (f"本日の観測を終了します。\n{det}\n明日も観測を続けます。")


def _compose(mode: str, date: str, since: float):
    from lib import llm
    if mode == "morning":
        return observe.enforce_regulations(_morning_text())  # 始業は戸田テンプレ（決定論・チャンネルリンク列挙）
    if mode == "progress":
        d = _progress_digest(since, date)
        prompt = ("Chiaki AI の毎時の観測進捗PDCAを3行で書いてください。数字は素材を使う。"
                  f"素材(過去1時間): 松永さんの報告{d['reports']}件、検知(表記{d['notation']}・誤字{d['typo']}・停滞{d['stall']})、"
                  f"裁定(GO/反映{d['go']}・却下{d['reject']})・修正完了{d['completed']}件。"
                  "1行目=報告(松永さんからの報告件数と、表記誤字・停滞の検知結果を1文で。"
                  "『過去1時間の観測報告です』のような前置き・メタ説明や、『過去1時間に』『この1時間で』のような"
                  "時間帯の語も書かない＝毎時なのは自明。件数と検知結果だけを本文から書く)、"
                  "2行目=詳細(数字で要点。すべて0なら『目立った動きはありません』等に丸めてよい)、"
                  "3行目=ラポート(引き続き何を見るか一言)。**3行＝3つの文（各行ちょうど1文・前置きで増やさない）**。"
                  "報告・詳細・ラポートの3部を、半角の ||| で区切って1行で返す。改行・前置き・絵文字なし、です/ます、各部1文で簡潔。")
    else:
        d = _close_digest(date)
        prompt = ("Chiaki AI の終業まとめPDCAを3行で書いてください。数字は素材を使う。"
                  f"素材: 実測{d['actuals']}件・未突合{d['unmatched']}件、検知(表記{d['notation']}・誤字{d['typo']}・停滞{d['stall']})、"
                  f"裁定(GO/反映{d['go']}・却下{d['reject']})・修正完了{d['completed']}件、未対応{d['open']}件。"
                  "1行目=報告(本日の観測を終了する旨を1文で)、2行目=詳細(数字で要点)、3行目=ラポート(所感・明日への一言)。"
                  "**3行＝3つの文（各行ちょうど1文・分かりきった前置きで増やさない）**。"
                  "報告・詳細・ラポートの3部を、半角の ||| で区切って1行で返す。改行・前置き・絵文字なし、です/ます、各部1文で簡潔。")
    tn = runtime.load_tuning("pdca")  # 戸田さんの口頭調整を反映
    if tn:
        prompt += " 戸田さんの指示（必ず守る）: " + "; ".join(tn) + "。"
    try:
        body = llm.haiku(prompt, max_tokens=300)
    except Exception as e:
        print(f"[chiaki-pdca] haiku failed: {e} -> deterministic fallback")
        body = None
    if not body:
        # Haiku 不通でも黙らない＝決定論の3行で継続（enforce は下の共通経路で掛かる）
        body = _fallback_lines(mode, d).replace("\n", "|||")
    # 区切り(|||/||/|/｜・前後スペース・改行)を全て改行に正規化＝3分割の成否に依存せず、生パイプを絶対に投稿しない。
    norm = re.sub(r"[ \t　]*[|｜]+[ \t　]*", "\n", body.strip())
    parts = [ln.strip() for ln in norm.split("\n") if ln.strip()]
    if not parts:
        return None
    if len(parts) > 3:  # 3行ルール: 余剰は詳細(2行目)へ畳む（報告=先頭・ラポート=末尾は保持）
        parts = [parts[0], "".join(parts[1:-1]), parts[-1]]
    out = runtime.ensure_punct(observe.enforce_regulations("\n".join(parts)))
    # enforce の URL前空行等で行が増えても3行に畳み直す（毎時/終業の3行を最終保証）
    lines = [ln.strip() for ln in (out or "").split("\n") if ln.strip()]
    if len(lines) > 3:
        lines = [lines[0], "".join(lines[1:-1]), lines[-1]]
    return "\n".join(lines) if lines else None


def main():
    if not runtime.is_jp_workday():
        print("[SILENT] holiday/weekend")  # 祝日に #5902 へ @channel を投げない（cron は曜日しか知らない）
        return
    now = _now()
    date = now.strftime("%Y-%m-%d")
    mode = _mode(now.hour)
    slot = mode if mode != "progress" else f"progress_{now.hour}"
    st = runtime.load_json("chiaki_pdca_state.json", {})
    day = st.setdefault(date, {})
    if day.get(slot):
        print(f"[chiaki-pdca] {slot} already posted for {date}")
        return
    since = float(day.get("last_post_ts") or 0.0)
    body = _compose(mode, date, since)
    if not body:
        print("[chiaki-pdca] compose failed")
        return
    # <!channel> は必ず別行（メンションの下で改行）。本文は _compose が必ず3行に畳むので @channel＋3行になる。
    source.post_message(runtime.CH_CHIAKI_PDCA, f"<!channel>\n{body}")  # top-level＋@channel
    now_ts = runtime.now_ts()
    day[slot], day["last_post_ts"] = now_ts, now_ts
    runtime.save_json("chiaki_pdca_state.json", st)
    print(f"[chiaki-pdca] posted {slot} for {date}")


if __name__ == "__main__":
    main()
