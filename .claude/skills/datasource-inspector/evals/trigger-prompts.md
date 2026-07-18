---
purpose: datasource-inspector のトリガー回帰テスト用プロンプト集
note: description・本文の修正時に fresh session で全プロンプトを確認する。運用中に観測された不発・誤発火はここに追記する
---

# トリガーテスト

## 起動すべき（should fire）

- このデータソースの現状を棚卸しして
- marts の PDS でどの列に説明が無いか調べて
- calc を追加する前に既存の計算フィールドを確認して
- この PDS の grain が設定されているか見て
- メタデータの整備状況をレポートして

## 起動すべきでない（should not fire）

- 列の説明の草案を作って（→ datasource-describer）
- 説明をデータソースに書き込んで（→ datasource-augmenter）
- workbook 間で重複している calc を探して（→ workbook-calc-prospector）
