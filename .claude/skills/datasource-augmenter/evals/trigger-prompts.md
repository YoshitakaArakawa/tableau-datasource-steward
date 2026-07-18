---
purpose: datasource-augmenter のトリガー回帰テスト用プロンプト集
note: description・本文の修正時に fresh session で全プロンプトを確認する。運用中に観測された不発・誤発火はここに追記する
---

# トリガーテスト

## 起動すべき（should fire）

- この change-set をデータソースに反映して
- marts の PDS に列の説明を書き込んで
- このデータソースに grain（粒度の説明）を設定して
- workbook から寄せた calc を PDS に追加して publish して
- ソース列の説明を Catalog に書いて全下流に継承させて
- さっき publish した __augmented の draft を掃除して
- さっきの desc 書き込みを巻き戻して

## 起動すべきでない（should not fire）

- どの列に説明が無いか棚卸しして（→ datasource-inspector）
- 列の説明の草案を作って（→ datasource-describer）
- workbook 間で重複している calc を探して（→ workbook-calc-prospector）
- 既存の説明が現データに合っているか点検して（→ datasource-describer）
