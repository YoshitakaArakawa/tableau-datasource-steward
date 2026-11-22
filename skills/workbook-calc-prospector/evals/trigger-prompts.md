---
purpose: workbook-calc-prospector のトリガー回帰テスト用プロンプト集
note: description・本文の修正時に fresh session で全プロンプトを確認する。運用中に観測された不発・誤発火はここに追記する
---

# トリガーテスト

## 起動すべき（should fire）

- この PDS を使っている workbook で重複している計算フィールドを探して
- 各 workbook に散らばった共通の計算を PDS に寄せられないか調べて
- marts の PDS の下流 workbook から hoist 候補を洗い出して
- どの calc が複数の workbook で同じ式になっているか調べたい

## 起動すべきでない（should not fire）

- PDS に既にある calculated field の一覧を見たい（→ datasource-inspector）
- 寄せる calc の説明文を書いて（→ datasource-describer）
- この calc を PDS に追加して publish して（→ datasource-augmenter）
- PDS に寄せた calc を使うように workbook 側を書き換えて（→ workbook-rewirer）
