---
purpose: datasource-describer のトリガー回帰テスト用プロンプト集
note: description・本文の修正時に fresh session で全プロンプトを確認する。運用中に観測された不発・誤発火はここに追記する
---

# トリガーテスト

## 起動すべき（should fire）

- この PDS の列の説明の草案を作って
- データ辞書を整備したい
- このデータソースの grain（何の 1 行か）を書いて
- 寄せた calc に説明を付けて
- 既存の説明が現データに合っているか点検して
- datasource の説明欄に build メモが入っているので grain に直したい

## 起動すべきでない（should not fire）

- どの列に説明が無いか棚卸しして（→ datasource-inspector）
- この change-set をデータソースに反映して（→ datasource-augmenter）
- workbook 間で重複している calc を探して（→ workbook-calc-prospector）
