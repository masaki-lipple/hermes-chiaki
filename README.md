# Hermes `management` プロファイル（chiaki / Lipple タスク管理脳）

Lipple の業務を観測する Hermes エージェント `management`（人格名 **chiaki**）のソース。
仕様: `management_soul_and_observation_spec_v2.md`。配備プラン: `~/.claude/plans/agile-munching-chipmunk.md`。

## 方針
- **開発・答え合わせはローカル（このリポジトリ／無課金）で完結 → 完成実装を VPS に rsync**。
- **決定論ファースト**: 観測の機械処理は `scripts/*.py`（LLM 不使用）。判断が要る時だけ LLM を起動。
- **観測は cron バッチ**（`--no-agent` スクリプト＝課金ゼロ）、gateway は #8902 の戸田対話/承認だけ。

## 構成
```
profile/                 # → ~/.hermes/profiles/management/ に同構成で配備
  SOUL.md                # 人格＋固定ガード（戸田固定 / Notion境界 / 投稿先 / 自己PDCA）
  config.yaml            # model分割 / gateway.slack / mcp_servers / memory
  .env.example           # 秘密テンプレ（実値はコミット禁止）
  honcho.json            # profile-local Honcho 設定
  skills/lipple/*/       # 観測8スキル＋補助（SKILL.md ＋ scripts/）
  state/                 # 実行時状態の JSON（schema/seed）
lib/                     # スクリプト共通ロジック（純Python・テスト対象）
fixtures/                # バックフィルした Slack/Notion 実データ（ローカル検証用）
tests/                   # fixtures に対する答え合わせ
```

## 監視/発信チャンネルと主要ID
| 種別 | 名前 | ID |
|---|---|---|
| 観測・人軸 | #5035-yu-pdca | `C09U4T1BBU0` |
| 観測・タスク軸 | #a027-日本自動ドア株式会社 | `C045C1ZBX26` |
| chiaki 作業ログ | #5902-chiaki-pdca | `C0BC6PPG013` |
| chiaki 窓口 | #8902-chiaki-management | `C0BCE19BN2G` |
| 承認者/許可ユーザー | 戸田 (Masaki Toda) | `U9R35H06L` |
| 観測対象の発信者 | 松永悠 / 根本(口調手本) / GCP同期bot | `U09T44VEZM1` / `U9UA8NQCB` / `U0BBZ3B3UNS` |

Notion: メンバーDB `028c62ed-3b31-495c-a5a9-33f1c7c7b595` / 用語辞書DB `0b7bd5e3-4db2-4959-b6f9-2ce37bd40e5e` / タスクDB（共有付与後に取得）。

## 観測ルール → 実装
| 仕様 | スクリプト | 種別 |
|---|---|---|
| §3.1 予定工数 / §3.4 リスケ | parse_schedule | 決定論 |
| §3.2 実測 / §3.3 予実 / §3.7 突合 | track_events | 決定論（突合失敗時のみ LLM） |
| §3.5 表記(辞書/Layer1) | notation_check | 決定論・課金ゼロ |
| §3.5 汎用誤字(Layer2) | typo-scan（相乗り＋17:50/18:30 Haikuバッチ） | LLM・極小 |
| §3.6 65分無音 | silence_check | 決定論・承認不要 |
| §3.8 停滞検知 | stall_scan（**活動=人間の反応のみ・GCP bot除外**） | 検知=決定論 |
| §3.3 相場 | compute_baselines | 決定論 |

Honcho peer は2つ: `toda`(対話・裁定を貯める) ＋ `y-matsunaga`(観測対象・§8評価の仕込み)。

## 🚩 配備前メモ
- Notion タスクDB（**🎯 タスク_DB**, ID `331980d4-f840-800b-8bde-f6669422aeb1`）は統合に**共有済み＝404解消**。書き戻し（status/sync_source 以外のメタのみ・承認後）は可能。観測本体は Slack+local 完結。
- 仕様の `#8902-chiaki-pdca` は実体 `#5902-chiaki-pdca`（要最終確認）。
