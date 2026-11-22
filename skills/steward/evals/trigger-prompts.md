---
purpose: steward のトリガー回帰テスト用プロンプト集
note: description・本文の修正時に fresh session で全プロンプトを確認する。運用中に観測された不発・誤発火はここに追記する
---

# トリガーテスト

## 起動すべき（should fire）

- この project の PDS の列説明をまとめて整備して
- データソースのメタデータ整備を一通り回して
- workbook 間で重複している calc を PDS に寄せて、workbook も付け替えて
- データソースの説明を整えたいけど、どの Skill を使えばいい？
- steward を使う前の準備（.env とサインイン）を確認して

## 起動すべきでない（should not fire）

- この PDS の grain が設定されているか見て（→ datasource-inspector）
- この列の説明の草案を作って（→ datasource-describer）
- Tableau のダッシュボードのデザインを相談したい
- workbook を新しく作って Tableau Cloud に公開して
