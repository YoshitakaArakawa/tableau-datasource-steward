---
name: workbook-calc-prospector
description: ある Published Data Source の下流 workbook を Metadata API で辿り、複数 workbook に重複するローカル calculated field を「PDS へ寄せる（hoist）候補」として検出する。formula 正規化で同値判定し、operand が PDS に存在するか・table calc や INCLUDE/EXCLUDE LOD でないか（いずれも view 文脈依存）で hoistability を分類する。共通化できそうな計算をデータソースに集約したい、どの calc が workbook 間で重複しているか調べたいときに使う。出力は datasource-augmenter にそのまま渡せる change-set 候補。
---

# workbook-calc-prospector

セマンティックレイヤー集約のための読取・分析 Skill。対象 PDS を使う workbook に散らばった**重複 calculated field** を見つけ、PDS へ寄せる候補を提示する。読取のみ（副作用なし）。workbook は書き換えない（steward は PDS へ寄せるまでが既定スコープ）。

## スコープ

含む:
- 対象 PDS の下流 workbook 列挙（Metadata API GraphQL）
- 各 workbook の埋め込み CalculatedField の formula 収集
- formula 正規化（**コメント除去 + 空白吸収**）での横断重複判定 → hoist 候補
- hoistability 分類（operand が PDS に存在 / table calc でない）
- 各候補に **WB 側の構造化 description（calc の `description` フィールド）** と **コメント込みの raw formula** を機械抽出して添付（describer 用の素材）

含まない:
- PDS への注入・publish（→ `datasource-augmenter`）
- workbook 側 calc の削除（スコープ外）
- formula の意味的等価判定（字句正規化のみ。`SUM([A])/SUM([B])` と等価な別表現は別物として扱う）
- **説明文の生成・コメントの意味判定**（→ `datasource-column-describer`。本 Skill は抽出のみで推論しない）

## hoistability の判断

候補のうち、次を**満たすものだけ `hoistable: true`**:
- formula の operand（`[field]`）がすべて対象 PDS のフィールドに存在する
- table calc 関数（`WINDOW_*`, `INDEX`, `LOOKUP`, `RUNNING_*`, `FIRST`, `LAST`, `RANK`, `TOTAL`, `SIZE`, `PREVIOUS_VALUE`）を含まない
- INCLUDE / EXCLUDE の LOD 式（`{ INCLUDE … }` / `{ EXCLUDE … }`）を含まない

table calc と INCLUDE / EXCLUDE LOD は、**view に含まれるディメンションによって計算結果が変わる**（view 文脈依存）。PDS の calc に寄せると view 次第で挙動が変わるため除外する。一方 FIXED LOD（`{ FIXED … }`）とディメンション省略のテーブルスコープ LOD（`{ MAX(…) }` 等）は view 文脈に依存しないので許容する。`hoistable: false` の候補も理由付きで出力し、判断はユーザーに委ねる。

## ワークフロー

進捗:
- [ ] 対象 PDS の LUID を確認（無ければ `datasource-inspector` / `list-datasources` で特定）
- [ ] `find_hoist_candidates.py --pds-luid <luid> --out candidates.json` を実行
- [ ] `candidates.json` を読み、`hoistable: true` の候補をユーザーに提示（formula・出現 workbook・operand）
- [ ] 採用する候補を `datasource-augmenter` の change-set `calcs[]` に変換して引き渡す

## 出力（change-set 候補）

`candidates.json`: `pds_name` / `downstream_workbooks` / `candidate_count` / `hoistable_count` / `candidates[]`。
各候補: `suggested_caption` / `formula`（コメント込み raw）/ `workbooks[]` / `workbook_count` / `operands[]` / `wb_descriptions[]` / `hoistable` / `reasons[]`。

`hoistable` の候補は augmenter の `calcs[]`（`caption` / `formula` / `datatype` 等）へ変換して渡す。`datatype` は formula から自明でないため、採用時にユーザー確認または inspector の型情報で補う。

### 説明素材の扱い（描画は describer へ）

`wb_descriptions[]` は各 WB calc の **構造化 description フィールド**を distinct 抽出したもの。`formula` はコメント（`//`, `/* */`）を残した raw。どちらも**抽出だけ**で、「コメントが説明か」「複数 description のどれを採るか」は判定しない。これらは `datasource-column-describer` の入力で、説明文の生成・衝突解決はそちらが担う。

- `wb_descriptions[]` が 0 件 → describer が formula（コメント含む）から草案を起こす（推論）。
- 1 件 → そのまま採用候補（抽出）。
- 2 件以上 → describer が衝突を解決し、解決できなければ orchestrator 報告でユーザー確認に回す。

## 認証 / 依存

OAuth（`scripts/tableau_auth.py` の `signed_in_server()`）。Metadata API は `scripts/metadata_api.py` の `graphql()` で叩く。依存: `tableauserverclient` / `python-dotenv` / `requests`。

## Scripts

| スクリプト | 役割 |
|---|---|
| `scripts/find_hoist_candidates.py` | 下流 workbook の calc を収集し、重複 formula を hoistability 付きで `candidates.json` に出力 |

## 設計原則

- 字句正規化で dedup（コメント除去後の formula をキーに、caption 非依存）。意味的等価は判定しない
- hoistable でない候補も理由付きで残し、判断はユーザー
- 読取のみ。注入は augmenter に委譲
- 説明は**抽出**まで（`wb_descriptions` / コメント込み raw formula）。生成・衝突解決は describer に委譲（推論を READ 層に持ち込まない）
