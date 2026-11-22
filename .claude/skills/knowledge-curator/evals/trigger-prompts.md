---
purpose: knowledge-curator のトリガー回帰テスト用プロンプト集
note: description・本文の修正時に fresh session で全プロンプトを確認する。運用中に観測された不発・誤発火はここに追記する
---

# トリガーテスト

## 起動すべき（should fire）

- この Business Preference を Knowledge に登録して
- BP 台帳を Knowledge と同期して
- データソースを上書きしたので、BP の参照切れを点検して
- Knowledge のルールが列の説明と重複していないか見て
- 「今年は会計年度」という決まりを Tableau Knowledge に入れたい
- スキルに書いたフィールド名がまだ実在するか確認して

## 起動すべきでない（should not fire）

- 列の説明の草案を作って（→ datasource-describer）
- 説明をデータソースに書き込んで（→ datasource-augmenter）
- どの列に説明が無いか棚卸しして（→ datasource-inspector）
- Knowledge を使ってこの問いに答えて（Knowledge の利用であり整備ではない）
