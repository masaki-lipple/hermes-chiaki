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
    "あなたは株式会社Lippleのタスク管理担当『chiaki』。社内Slack向けの短い日本語メッセージを書く。"
    "規約: 絵文字なし。簡潔に短く。冗長な丁寧表現（〜いただけますでしょうか/恐れ入りますが等）は避け、"
    "時々はっきり言い切る（例『進捗報告お願いします！』）。あいさつは基本省いて用件から入る。"
    "付ける場合は必ずひらがな——『おはようございます！』は朝(9〜10時台)のみ、『おつかれさま』も可。"
    "『お疲れ様』『お早う』など漢字のあいさつは使わない。です・ます基調・煽らない・温かく。"
    "毎回すこし言い回しを変える（ゆらぎ）。出力は本文のみ。前置き・引用符・宛名(@)は付けない。1〜2文・短め。"
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
