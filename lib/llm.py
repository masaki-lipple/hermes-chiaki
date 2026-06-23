"""軽量 LLM ヘルパ（決定論スクリプトから「文面のゆらぎ」だけを Haiku で生成する用）。
標準ライブラリのみ。ANTHROPIC_API_KEY は launcher が profile .env からロード。
判断は呼ばない——短い文面生成だけ。失敗時は呼び側が固定文へフォールバックする前提。
"""
from __future__ import annotations
import json
import os
import urllib.request

MODEL_HAIKU = "claude-haiku-4-5"

# chiaki のトンマナ（SOUL のトンマナ規約に対応。戸田さんの調整時はここと SOUL を更新）
CHIAKI_TONE = (
    "あなたは株式会社Lippleのタスク管理担当『chiaki』。社内Slack向けに短い日本語メッセージを書く。"
    "規約: 絵文字は使わない。定型の挨拶（お疲れさまです等）は基本省いて用件から入る"
    "（朝＝9〜10時台のみ『おはようございます！』を付けてよい）。丁寧（です・ます）だが簡潔。"
    "煽らない・責めない・温かく。毎回すこし言い回しを変える（ゆらぎ）。"
    "出力は本文のみ。前置き・引用符・宛名(@)は付けない。1〜2文。"
)


def haiku(user: str, system: str = CHIAKI_TONE, max_tokens: int = 150) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    body = {"model": MODEL_HAIKU, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": user}]}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    res = json.load(urllib.request.urlopen(req, timeout=20))
    parts = [b.get("text", "") for b in res.get("content", []) if b.get("type") == "text"]
    return "".join(parts).strip()
