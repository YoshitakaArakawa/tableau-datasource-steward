---
purpose: workbook-rewirer のトリガー回帰テスト用プロンプト集
note: description・本文の修正時に fresh session で全プロンプトを確認する。運用中に観測された不発・誤発火はここに追記する
---

# トリガーテスト

## 起動すべき（should fire）

- PDS に寄せた calc を、ワークブック側でも実際に使うように書き換えて
- ワークブックのデータソースを augmented PDS に付け替えて動作確認したい
- hoist した計算式のローカル版を workbook から消して、PDS の calc を参照させて
- rewired 版の workbook を作って view が壊れていないかテストして

## 起動すべきでない（should not fire）

- どの calc が workbook 間で重複しているか調べて（→ workbook-calc-prospector）
- 計算フィールドを PDS に追加して publish して（→ datasource-augmenter）
- 列の説明文の草案を作って（→ datasource-describer）
- workbook のフィルタとレイアウトを直して（view 編集はスコープ外）
