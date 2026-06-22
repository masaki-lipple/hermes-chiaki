---
name: apply-ruling
description: #8902 で戸田さんが提案に下す裁定（GO/却下/文面修正）を拾って実行し、結果をHoncho戸田peerとMEMORYに学習させる。gateway イベント駆動。
metadata:
  hermes:
    tags: [judgment, approval, gateway, llm]
---

# apply-ruling（§6 裁定の実行＋学習）

`#8902-chiaki-management` は gateway の `free_response_channels`＝戸田さんの発言を拾う。提案スレッドへの戸田さんの返信が来たら起動。

## 手順
1. 返信者が **戸田さん（`U9R35H06L`）本人**か確認（他者なら無視）。
2. 親 `thread_ts` で `scripts/apply.py find <thread_ts>` し pending_approval を引く。
3. 返信を分類:
   - **GO/OK** → `draft_text` を**対象スレッド**（source_channel + source_thread_ts）に @メンションで投稿。
   - **文面こう/修正** → 戸田さんの指定文面で投稿。
   - **却下/流して** → 投稿しない。
4. `scripts/apply.py resolve <thread_ts> <go|reject|edit> "<final_text>"` で pending を解消。
5. **学習**: 裁定（GO/却下＋理由＋文面の直し）を Honcho 戸田peer に記録（`.chat()`/profile 更新）し、繰り返す型は `apply.py memo "<一行>"` で `MEMORY.md` に蓄える。却下が続く型は「流す」へ寄せる＝閾値が育つ。

## 卒業
- 裁定が安定したら戸田さんが policy をゲート off に。以後は propose が直接投稿＋事後報告に切替（apply-ruling は事後微調整の窓口に縮小）。
