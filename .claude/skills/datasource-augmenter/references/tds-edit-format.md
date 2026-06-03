---
purpose: datasource-augmenter が .tds XML を編集する際の構造仕様（field description と calculated field の注入形）
sources:
  - https://help.tableau.com/current/api/rest_api/en-us/REST/rest_api_ref_data_sources.htm
note: 注入する XML 形と配置・escape・検証点を定義する。rename / hide / cast / default-agg / folder は現行スコープ外
---

# TDS XML 編集フォーマット

## 目次
- 改行コード（CRLF 注意）
- field の特定（caption と name）
- field description（`<desc>`）の注入
- calculated field の注入
- .tds の層構造（どの層が consumer に効くか）
- 検証点（round-trip）

## 改行コード（CRLF 注意）

`.tds` は LF とは限らず **CRLF のことがある**。改行に依存した anchor（`/>\n` のような）は CRLF で一致せず編集が無言で抜ける。**改行非依存**で挿入する（タグ文字列を anchor にして直後に挿入）。

## field の特定（caption と name）

datasource 直下の `<column>` は、display 名と内部 `name` が異なるときだけ `caption='X'` を持つ。等しい場合は `caption` が省略され `name='[X]'` のみになる。よって display 名で特定するには **`caption='X'` または `name='[X]'` の両方**を試す。

## field description（`<desc>`）の注入

`<column>` の子に置く。中身は `<formatted-text><run>` のリッチテキストコンテナ。

```xml
<column datatype='string' name='[Category]' role='dimension' type='nominal'>
  <desc>
    <formatted-text>
      <run>Top-level product grouping</run>
    </formatted-text>
  </desc>
</column>
```

- 対象列が self-closing（`<column ... />`）なら open タグ + `<desc>` + `</column>` に展開する
- 既存 `<desc>` があれば置換する（重複させない）
- `<run>` のテキストは XML escape（`& < > ' "`）する

直接書き込む REST / Metadata API は無いため、この XML 注入 + republish が唯一の汎用経路。**calc field の説明もこの経路でのみ付与できる**（calc には継承元の上流列が無い）。

## calculated field の注入

`<datasource>` 直下、`<aliases .../>` の直後に、他 `<column>` の sibling として置く。

```xml
<aliases enabled='yes' />
<column caption='Profit Ratio' datatype='real' name='[Calculation_steward_1]'
        role='measure' type='quantitative'>
  <calculation class='tableau' formula='SUM([Profit])/SUM([Sales])' />
  <desc>
    <formatted-text><run>利益率</run></formatted-text>
  </desc>
</column>
```

| 属性 / 子 | 値 |
|---|---|
| `caption` | display 名 |
| `name` | XML 内 ID。`[Calculation_<id>]` 形式。複数注入時は連番で衝突回避 |
| `datatype` | `real` / `integer` / `string` / `boolean` / `date` / `datetime` |
| `role` / `type` | `measure`+`quantitative` / `dimension`+`nominal` 等 |
| `<calculation>` | `class='tableau'`、`formula` は XML escape 済みの Tableau Calc 式 |
| `<desc>`（任意） | calc 自体の説明 |

formula 内の `'` は `&apos;`、`<` は `&lt;` 等にエスケープする。日本語列名・式はそのまま UTF-8 で書ける。

## .tds の層構造（どの層が consumer に効くか）

| 層 | consumer への効き |
|---|---|
| `<column caption>` | 全 consumer（VizQL / Workbook） |
| `<column>` 子 `<desc>` | 通常列は `get-datasource-metadata` の `description` に露出、全層で説明として見える |
| `<column>` 子 `<calculation>` | 新規 field として全層に露出（GraphQL で CalculatedField として確認可） |
| `<column datatype>` 単独変更 | Desktop UI のみの cosmetic（VizQL に届かない）→ 本 Skill では使わない |
| `<metadata-records>` | source-of-truth。サーバーが上書きするため触らない |

## 検証点（round-trip）

publish 後に再 DL した `.tds` で確認する:

1. 注入した `<desc>` の `<run>` 本文が残る（通常列・calc 列とも）
2. 注入した calc の `<column>` と `<calculation formula>` が残る
3. `<formatted-text>/<run>` 構造が保持される

calc は `get-datasource-metadata` には現れない（物理列のみ列挙）ため、GraphQL Metadata API（`fields{ ... on CalculatedField{ name formula } }`）で登録を確認する。
