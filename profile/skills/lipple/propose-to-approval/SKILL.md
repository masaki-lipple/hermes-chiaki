---
name: propose-to-approval
description: findings（判断候補：表記・誤字・ラベル・停滞の促し方）を戸田さんの像（Honcho）に照らして取捨し、#8902-chiaki-management に提案を出す。サイレント期の承認ゲート本体。
metadata:
  hermes:
    tags: [judgment, approval, llm]
---

# propose-to-approval（§6 サイレント期の承認ゲート）

obs-batch / typo-scan / stall-scan が積んだ `findings.jsonl` を処理する判断パス。**ここで初めて LLM を使う**（観測本体は決定論）。

## 手順
1. `scripts/queue.py list` で `status:new` の findings を取得。
2. 各 finding について **Honcho の戸田さん peer に `.chat()`**「戸田さんはこの種のズレを促す人か？（例: 納品物の誤字は言う／PDCA本文の軽微typoは流す）」で取捨。流すなら `queue.py mark <id> skipped`。
3. 促す価値ありなら**文面案**を作る（根本さんの口調・です/ます・理由を添える・煽らない）。例:
   > 「松永さんの〇〇報告に sns→SNS を促したいです。理由：表記レギュレーション。文面案：『SNS表記は大文字で統一でお願いします🙏』。[スレッドへのリンク]」
4. `#8902-chiaki-management` に提案を投稿（**@戸田メンション**で通知担保）。`scripts/queue.py pending <finding_id> <proposal_ts> <source_channel> <source_ts> "<draft>"` で `pending_approvals.json` に記録。`queue.py mark <id> proposed`。
5. 裁定は apply-ruling が拾う（戸田さんのスレッド返信）。

## 本番期（policy.json でゲート off）
- `quality_nudges_require_approval:false` 等になったら、**承認を待たず直接**対象スレッドへ促し、**事後に #8902 へ報告**（リンク＋理由）。

## cron
`0 9-19 * * 1-5`（営業時間に毎時）。findings が空なら即 `[SILENT]`（コスト極小）。安全網として24h未裁定の提案を #8902 に再掲。
