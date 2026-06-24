# Hermes `management` プロファイル（Chiaki AI / Lipple タスク管理脳）

Lipple の業務を Slack 上で観測する Hermes プロファイル `management`（人格名 **Chiaki AI**）のソース。
PDCA チャンネルを観測し、**決定論スクリプト＋Haiku（文面・判断のみ）**で動く観測ボット。
会話エージェント（execute_code 等のツール）は安全のため**持たない**。

> 原本仕様書 `management_soul_and_observation_spec_v2.md` は現存しない。仕様の実装形はこの README ＋ `profile/SOUL.md` ＋ `CLAUDE.md` に集約。経緯・ID・決定の詳細は Claude のプロジェクトメモリ `project-chiaki-hermes`。

## 方針
- **開発・検証はローカル（このリポジトリ）で完結 → 完成実装を VPS に同期**。
- **決定論ファースト**：観測の機械処理は `scripts/*.py`（LLM 不使用＝課金ゼロ）。文面・判断・分類が要る時だけ Haiku を起動。
- **安全第一**：自走するコード実行エージェントは置かない。判断は決定論、文面は Haiku、コード変更は人間（AI コーディングエージェント）が承認付きで行う。

## アーキテクチャ（現行）
- 本番は VPS（`ssh -i ~/.ssh/hermes_vps chiaki@220.158.22.130`）。GitHub `masaki-lipple/hermes-chiaki`（private）。反映は **`~/deploy.sh`**（git pull＋コード同期）。
- 実行基盤は **ユーザー crontab（cron デーモン）＋ Socket Mode listener** の2本立て。
  - **cron**：時間ベースの観測・発信（13 ジョブ、下記）。
  - **listener**（`chiaki-listener.service`／systemd user／`Restart=always`／linger）：#8902/#5035/#a027 の関連スレッド返信を即時受信し、決定論スキルを起動（≤2秒）。`flock` で cron と排他。
- ⚠️ **gateway（`hermes-gateway-management.service`）は廃止・無効のまま。絶対に起動しない**（LLM 会話エージェントが復活し execute_code を暴走させた経緯）。
- Slack アプリは **「Chiaki AI」（`@chiaki_ai`／bot user_id `U0BCCMPKD54`）** に作り直し済み。
- 外部 API は全て urllib（Slack / Notion）＋ `lib/llm.py`（Haiku）。実行時 MCP サーバ不要。

## 構成
```
profile/                 # → ~/.hermes/profiles/management/ に同構成で配備
  SOUL.md                # 人格＋固定ガード（戸田固定 / Notion境界 / 投稿先 / メンション方針）
  config.yaml / .env.example / honcho.json
  skills/lipple/*/       # 11 スキル（SKILL.md ＋ scripts/run.py）
  state/                 # 実行時状態の JSON（tuning / pending / findings / rulings / cursor 等）
  scripts/               # cron / listener ランチャ（.env 自己ロード・stdlib のみ）
lib/                     # 共通ロジック（純Python・テスト対象）
  runtime / source / observe / llm / notion / sync_notation
fixtures/ tests/         # 実データ・答え合わせ
CLAUDE.md                # AI コーディングエージェント向け運用規約（修正フロー・トーン規約）
```

## チャンネル / 主要 ID
| 種別 | 名前 | ID |
|---|---|---|
| 観測・人軸（PDCA） | #5035-yu-pdca | `C09U4T1BBU0` |
| 観測・タスク軸（業務） | #a027-日本自動ドア株式会社 | `C045C1ZBX26` |
| chiaki 自己PDCA発信 | #5902-chiaki-pdca | `C0BC6PPG013` |
| chiaki 窓口（提案/裁定/報告） | #8902-chiaki-management | `C0BCE19BN2G` |
| 承認者・対話相手 | 戸田（Masaki Toda） | `U9R35H06L` |
| 観測対象の発信者 | 松永悠 / 根本（口調手本） / GCP同期bot | `U09T44VEZM1` / `U9UA8NQCB` / `U0BBZ3B3UNS` |
| Chiaki AI 自身（セルフメンション用） | Chiaki AI | `U0BCCMPKD54` |

Notion: 🎯 タスク_DB `331980d4-f840-800b-8bde-f6669422aeb1` / 用語辞書 / メンバー / レギュレーション / **Chiaki_AI Issue_DB**（hard 起票先）`0bccce01dd944be4901d95e950a3964c`。いずれも統合「Hermes Agent」へ共有済み。

## スキル（11）
| スキル | 役割 | 対象 |
|---|---|---|
| obs-batch | 予定/実測/予実/表記(辞書) の観測 | #5035 |
| silence-reminder | 65分無音リマインド（決定論・承認不要） | #5035 |
| stall-scan | タスクスレッドの停滞検知（活動＝人の反応のみ） | #a027 |
| typo-scan | 自由文の誤字検知（Haiku・保守的） | #5035 |
| compute-baselines | 工数相場の算出 | — |
| propose-to-approval | findings(表記/誤字/停滞) を #8902 に「提案」 | #8902 |
| apply-ruling | 裁定実行＋完了追跡＋未完了リマインド | #8902→#5035/#a027 |
| chiaki-pdca | 自己PDCA（9時計画/毎時/18時終了・@channel・3行） | #5902 |
| chiaki-intake | 指摘の起票1窓口（@メンション→issue/rule 振り分け→確認→起票・質問回答） | @メンション全般・#8902・#5902 |
| event-listener | Socket Mode 即時起動デーモン（apply-ruling / chiaki-intake） | 常駐 |
| notion-write | タスクDB 補完メタ書き込み（status/sync_source は不可侵） | Notion |

## cron（13・JST）
```
*/10 9-19 平日   obs_batch          観測バッチ
0 9   平日       stall              停滞の日次チェック(#a027)
0 21  平日       baselines          相場算出
0 8   毎日       sync_notation      Notionレギュレーション→state同期
*/2   毎時       silence            65分無音リマインド(#5035)
0 9-19 平日      propose            findings→#8902 提案
*/1 9-19 平日    apply_ruling       裁定/完了追跡(flock)
50 17 / 30 18    typo_scan          誤字バッチ(Haiku・1日2回)
0 9 / 0 10-17 / 0 18 平日  chiaki_pdca   自己PDCA→#5902
*/2 9-19 平日    chiaki_intake      指摘の起票backstop(flock)
```

## 主要フロー
1. **観測→提案→裁定→促し→完了追跡**：obs-batch/typo-scan/stall-scan が検知 → `findings` → propose が #8902 に「提案：表記/誤字/停滞」＋下書きを投稿（pending 記録）→ **戸田が同スレッドに「GO / 却下 / 自然文の文面修正」を書く（@メンション不要）** → apply-ruling が実行（GO＝下書きそのまま／文面修正＝Haiku で反映）→ 対象スレッド(#5035/#a027)へ @対象者 で促し → `awaiting_completion` → 対象者の完了報告で **松永さんへお礼＋戸田さんへ完了通知**（該当箇所リンク）→ `completed`。未完了は 120 分ごと最大 2 回リマインド。文面修正は `style_corrections` に学習し提案下書きに few-shot 反映。
2. **即時化**：listener が関連スレッド返信を受けて apply-ruling / chiaki-intake を即起動。cron は時間ベース処理＋バックストップ。
3. **自己PDCA**：chiaki-pdca が #5902 に 1時間ルールで投稿（9時計画／10-17毎時進捗／18時終了、@channel、報告/詳細/ラポートの3行）。
4. **指摘の起票（@メンション1窓口・着手C）**：戸田の @メンション/#8902/#5902 投稿を Haiku が振り分け、**必ず一度きいてから**起票（2ターン・振り分けは上書き可）。会話エージェントは使わない。
   - **rule**（言葉のルール＝旧 soft：トーン/用語/表記）→ 案提示→確認→ Rule Registry（未承認）。承認→正本（用語/レギュレーション/Style）→翌日 sync で反映。
   - **issue**（不具合・要望＝旧 hard：バグ/変更/新機能）→ 案提示→確認→ Chiaki_AI Issue_DB（未対応）。Claude Code でのバグ潰しバックログ。
   - **edit**→その場で編集／**question**→Haiku がテキスト回答／**unclear**→確認質問／**none**→何もしない（ツール無し＝安全）。
5. **修正報告フロー**（コーディングエージェント側）：コードを直したら **#8902 に「報告：コード修正」を投稿（一覧把握）＋ 該当スレッドにも実態の修正を残す（どこで何を直したか）＋ Issue を「完了」に**。**サイレント修正・削除は禁止**。詳細は `CLAUDE.md`。

## 観測ルール → 実装
| 仕様 | スクリプト | 種別 |
|---|---|---|
| §3.1 予定工数 / §3.4 リスケ | parse_schedule | 決定論 |
| §3.2 実測 / §3.3 予実 / §3.7 突合 | track_events（突合失敗時のみ LLM） | 決定論 |
| §3.5 表記（辞書・Layer1） | notation_check（1文字漢字ルールは直前が仮名のときだけ＝記事/行為の誤検知回避） | 決定論 |
| §3.5 汎用誤字（Layer2） | typo-scan（Haiku・1日2回） | LLM・極小 |
| §3.6 65分無音 | silence_check | 決定論・承認不要 |
| §3.8 停滞 | stall_scan（活動＝人の反応のみ・GCP bot 除外） | 検知＝決定論 |
| §3.3 相場 | compute_baselines | 決定論 |

## トーン規約（レギュレーション）
- です・ます調だが過剰にへりくだらない（「了解です」一辺倒にしない）。
- 感嘆符は全角「！」。各文末に句読点「。」。太字（`*〜*`）は使わない。括弧は「」優先。
- 表記の決まりは「レギュレーション」と呼ぶ。
- 英数字・記号（#等）と日本語の境目に半角スペースを入れない（固有名詞内は残す。例: `Claude Codeによる`／`#5902投稿`）。
- 確認を取りたい相手＝`<@戸田>`（ping）、chiaki 自身の処理・独り言＝セルフメンション。
- 数字・時間は正確に。意味不明な文を出さない。

## 安全・固定の決定事項
- 会話エージェント／コード実行ツールは持たない。⚠️ gateway は起動しない。
- 秘密情報は VPS `.env` とローカル `~/.config/hermes-chiaki/secrets.env`（repo 外・コミット禁止）。
- Notion 書き戻しは `status` / `sync_source` に触れない（カテゴリー/工数/優先度のみ・承認後）。
- 呼称は「戸田」（「Masaki」と書かない）。

## 残バックログ
- **backfill**：1〜2ヶ月分の実データを取り込み、相場（baselines）を充実させる。
- **Notion 書き戻し**：タスクDB のカテゴリー/工数/優先度を承認後に補完。
