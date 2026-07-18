---
purpose: workbook-rewirer が .twb XML を編集する際の構造仕様（published datasource の repoint と calc 参照差し替えの形）
sources:
  - https://github.com/tableau/tableau-document-schemas
  - https://github.com/tableau/document-api-python
note: 編集対象の要素・置換規則・検証点を定義する。view レイアウトの編集はスコープ外。XSD は connection 内容と calc 内容を processContents="skip" で検証しないため、実 .twb（DL した original.twb）と publish 後の view 描画検証を実務上の正とする
---

# TWB XML 編集フォーマット

## 目次
- 編集対象の構造（published datasource ブロック）
- 参照の形（token と qualified name）
- 編集 1: ローカル calc 定義の削除
- 編集 2: repoint（repository-location の付け替え）
- 編集 3: token 置換と dependency キャッシュの掃除
- 検証点

## 編集対象の構造（published datasource ブロック）

.twb 冒頭の `<datasources>` に、published datasource ごとの定義ブロックがある:

```xml
<datasource caption='My PDS' inline='true' name='sqlproxy.0abc...' version='18.1'>
  <connection channel='https' class='sqlproxy' dbname='My PDS' ... >...</connection>
  <column caption='Profit Ratio' datatype='real' name='[Calculation_123456]'
          role='measure' type='quantitative'>
    <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
  </column>
  <repository-location id='MyPDSContentUrl' path='/t/site/datasources' revision='1.0' site='site' />
</datasource>
```

- **workbook 自体の `<repository-location>`**（path が `.../workbooks`）もファイル先頭にある。datasource のものは path が `/datasources` で終わることで区別する。
- ブロック内の `<column>` + `<calculation>` が **workbook ローカルの calc**。PDS 側 field はここに現れない（使用分が worksheet の dependency キャッシュに写るだけ）。
- worksheet 内にも `<datasource caption='...' name='sqlproxy...' />` の**自己完結タグの参照**が出るが、これは定義ではない。定義ブロックは `<repository-location>` を含む開きタグ形。

## 参照の形（token と qualified name）

field の内部名 token（`Calculation_123456` 等）は .twb 全体で次の形で参照される:

| 形 | 出現場所 |
|---|---|
| `[Calculation_123456]` | `<column name>` / `column-instance` の `column` 属性 / formula 内 |
| `[none:Calculation_123456:qk]` | `column-instance` の `name` 属性（`<derivation>:<token>:<suffix>` 形式） |
| `[sqlproxy.0abc...].[none:Calculation_123456:qk]` | shelf / style 等の datasource 修飾つき参照 |

いずれも token は `[` `]` `:` で区切られる。**区切り文字に挟まれた token だけを置換**すれば全参照形をカバーでき、無関係なテキストに波及しない（lookaround `(?<=[\[:])token(?=[\]:])`）。

## 編集 1: ローカル calc 定義の削除

定義ブロック内の該当 `<column>`（`<calculation>` 子を持つもの）を要素ごと削除する。

- 特定は **正規化 formula の一致**（コメント除去 + 空白吸収。prospector と同じ正規化）で行う。caption は workbook ごとに揺れるため使わない。
- 削除だけでは view が壊れる（参照が宙に浮く）。必ず編集 3 の token 置換とセットで行う。

## 編集 2: repoint（repository-location の付け替え）

workbook が旧 PDS を向いたまま augmented PDS の calc を参照しても解決しない。接続先が違うときだけ、定義ブロック内を付け替える:

| 箇所 | 置換 |
|---|---|
| `<repository-location id='...'` | 旧 PDS の content_url → 新 PDS の content_url |
| `<repository-location ... revision='...'` | **属性ごと削除**（旧 PDS のリビジョン固定は新 PDS では無意味。無指定 = 最新） |
| `<datasource caption='...'` | 新 PDS の表示名 |
| `<connection class='sqlproxy' ... dbname='...'` | 新 PDS の表示名 |

- 同一 site 内の付け替えを前提とする（path / site 属性は触らない）。
- dbname が表示名を持つのは実 .twb の観測に基づく仮定（XSD は connection 属性を検証しない）。Document API（document-api-python）も connection の dbname / server 書き換えで同種の付け替えを行っている。置換件数を result に記録し、view 描画検証で最終確認する。
- workbook が複数の published datasource を使う場合は、どのブロックを付け替えるか `source_pds_luid` で一意化する。

## 編集 3: token 置換と dependency キャッシュの掃除

1. 旧 token → PDS 側 calc の token（PDS の .tds から caption で引く）を **.twb 全体**で区切り文字保護つき置換する。
2. worksheet の `<datasource-dependencies>` に残る該当 `<column>` のキャッシュ写しから `<calculation>` 子を削除する。定義は削除済みなので、キャッシュに formula が残っていると client がローカル calc として扱い続ける余地がある。

## 検証点

publish 後に確認する（実行は同梱スクリプトが行う）:

1. **round-trip**: 再 DL した .twb で旧 token が消え、新 token と新 content_url が残る
2. **view 描画**: 全 view の CSV エクスポートが成功する（サーバー側でクエリが実行されるため、参照切れ・formula 不整合はここで露見する。XML 検証だけでは field 解決の成否は分からない）
3. **GraphQL（補助）**: workbook の upstream に対象 PDS が入り、embedded calc から hoist 済み formula が消えている。Metadata API のインデックス遅延があるため合否ゲートには使わない
