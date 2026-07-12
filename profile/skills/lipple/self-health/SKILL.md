---
name: self-health
description: 毎朝の自己点検。listener生存・cron生存（実行痕跡）・台帳鮮度・「listenerが受信したのに処理痕跡が無い」黙殺の検知。異常時だけ#8902へ警告。
metadata:
  hermes:
    tags: [observation, deterministic, cron]
---

# self-health（毎朝の自己点検・2026-07-10 戸田GO）

「なぜ無視される」（a040の@メンション黙殺）の再発防止層。決定論・LLM非起動。

1. **listener生存**: `systemctl --user is-active chiaki-listener.service`。
2. **cron生存**: 前回点検以降の cron.log 差分に、毎回必ずログを出すスキルの実行痕跡（`[intake]` 等）があるか。
3. **台帳鮮度**: task_ledger.json（stall-scan・平日9:00）が前営業日9時以降に更新されているか。
4. **黙殺検知**: listener が受信・起動を記録した listener_dispatch.jsonl と、intake の処理痕跡
   （chiaki_intake.json の items／tuning_cursor.json の前進）・codex_threads の既読位置を突き合わせ、
   「受けたのに処理された形跡が無い」イベントを警告（直近10分は処理中の可能性＝次回に回す。
   裁定スレッド宛ては apply-ruling の領分＝対象外）。

異常があるときだけ #8902 へ `<@戸田>` 警告。正常時は無音（cron.log に `[self-health] ok` のみ）。

## cron
`40 8 * * 1-5`（稼働開始前に前日ぶんを総括）。
