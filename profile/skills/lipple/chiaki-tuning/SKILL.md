---
name: chiaki-tuning
description: 戸田さんの @メンション/投稿を Haiku で issue/rule に振り分け、必ず一度きいてから Issue_DB(未対応)/Rule Registry(未承認) に起票する1窓口。会話エージェントは使わない（安全）。
metadata:
  hermes:
    tags: [intake, issue, rule, llm, approval]
    model: claude-haiku-4-5
---

# chiaki-tuning（指摘の起票経路・@メンション1窓口）

戸田さんの指摘を**1つの窓口**で受け、Chiaki AI が Issue か Rule かを判定して**必ず一度きいてから**該当 Notion DB に未対応/未承認で起票する（着手C）。会話エージェントは持たない（決定論＋Haiku のみ）。

## 使い方（戸田さん）
- `@Chiaki AI 〜` とメンションするだけ（**どこでも・何でも**）。Chiaki AI の発話に直接スレッド返信で `@Chiaki AI これ固いね` でもよい。
- #8902/#5902 では @メンション無しの投稿も拾う。内容は不具合でも言葉のルールでも可。振り分けは Chiaki AI が判定し、戸田さんが上書きできる。

## 振り分け（Haiku）
- **issue**（不具合・要望＝旧hard）→ `Chiaki_AI Issue_DB`（`0bccce01…`・未対応・種別 バグ/変更/新機能/その他）。
- **rule**（言葉のルール＝旧soft：トーン/用語/表記）→ `Rule Registry_Hermes Agent__DB`（database_id `e10777d5a7a04ac294273b9e077e1a38`・未承認・種別 用語/レギュレーション/スタイル・起票者/対象=chiaki）。
- **edit**（この投稿を今すぐ直して）→ その場で chat.update（起票しない）。
- **question** → テキスト回答。 **unclear** → 種別が曖昧な指摘＝まず確認質問。 **none** → 雑談/お礼等＝何もしない。

## 2ターン（必ずきいてから起票）
1. 案提示：「Issueに『…／種別=…』で / Ruleに『…』で 登録しておきますね？」（unclear は確認質問のみ・起票しない）。
2. 戸田確認（同スレッド）：OK→起票／文面修正・振り分け変更（「それRuleね」）→再分類して再提示／却下→見送り。
3. 起票 → 「登録しました！」＋ページURL。起票元＝メンション投稿の permalink。
- 承認→正本反映（用語辞書/レギュレーション/Style）は別フロー（人が承認）。**未承認/未対応は本番不反映**。

## 起動・状態
- `event-listener` が即時起動（@メンション＝どこでも／確認待ちスレッド返信／`event_id` で重複排除）＋ cron `*/2 9-19` backstop。flock `/tmp/chiaki_tuning.lock`。
- 状態 `chiaki_intake.json`（案→確認の2ターン・status awaiting_confirm/filed/cancelled/expired・24h失効）。返信は焼いたスタイル（`llm.haiku` system）＋`observe.enforce_regulations`＋句読点。
- 旧 soft→`tuning.json` 自動学習は廃止（rule として Rule Registry へ＝承認ゲート経由・lang-rules 正本モデルと整合）。生成側（silence/pdca/propose）の既存 `tuning.json` は凍結利用。
