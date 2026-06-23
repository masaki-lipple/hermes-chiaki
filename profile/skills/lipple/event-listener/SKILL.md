---
name: event-listener
description: Slack Socket Mode で #8902/#5035/#a027 のスレッド返信を即時受信し、決定論 apply-ruling を起動する常駐デーモン。会話エージェントは使わない（安全）。
metadata:
  hermes:
    tags: [realtime, socket-mode, deterministic]
---

# event-listener（即時トリガー・会話エージェント無し）

`apply-ruling` は通常 crontab（1分間隔）で回るが、戸田さんの裁定や松永さんの完了報告に**投稿した瞬間に反応**させたい場合のための常駐リスナー。

## 設計（安全第一）
- Socket Mode でメッセージイベントを受信するだけ。**LLM 会話エージェントは一切起動しない**（gateway の暴走とは別物）。
- 受信イベントが **pending に関係するスレッド**（#8902 の提案スレッド＝戸田さんの裁定／#5035・#a027 の対象スレッド＝対象者の完了報告）への**スレッド返信**のときだけ、決定論 `apply-ruling` を即実行。
- bot 自身・編集(subtype)・無関係スレッドは無視。
- crontab の apply-ruling とは **flock（`/tmp/chiaki_apply.lock`）で排他**＝二重処理しない。crontab はバックストップ（リスナー停止時の保険）＋時間ベースの再リマインド用に継続。

## 配備
- 依存: `slack_sdk`（builtin Socket Mode は stdlib のみで websocket 接続）。専用 venv `~/.hermes/listener-venv` に導入。
- launcher: `~/.hermes/profiles/management/scripts/listener.py`（env/.env をロードしてこの run.py を exec）。
- 常駐: user systemd `chiaki-listener.service`（Restart=always・linger）。
- crontab の apply-ruling 行は `flock -n /tmp/chiaki_apply.lock ...` でラップ。

## 注意
- gateway（hermes-gateway-management）は会話エージェントを含むため **無効のまま**。本リスナーはその代替ではなく「安全なイベント受信だけ」を担う。
