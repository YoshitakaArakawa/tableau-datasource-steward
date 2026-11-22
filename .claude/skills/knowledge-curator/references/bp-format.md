---
purpose: Tableau Knowledge の Business Preference（BP）の書式と、BP 台帳のスキーマ・点検結果の意味
sources:
  - https://tableau.github.io/tableau-mcp/docs/web-tools/knowledge/manage-knowledge-context
  - https://tableau.github.io/tableau-mcp/docs/web-tools/knowledge/inspect-knowledge-context
  - https://tableau.github.io/tableau-mcp/docs/web-tools/knowledge/query-knowledge-context
fetched_at: 2026-11-22
source_last_known_update: 不明
note: BP を書く・直す・点検するときの正典。どの文を BP にするかの判定（説明か BP か）は扱わず、BP にすると決まった文の書き方だけを扱う。
---

# BP の書式と台帳

## 目次
- BP に置くもの
- 文面の書式
- 名前の書き方
- 台帳のスキーマ
- 点検結果の意味

## BP に置くもの

BP は「どう使うか」を組織で共有する決まり。次の4種だけを置く。

| kind | 規範の番号 | 書くこと |
|---|---|---|
| `Usage` | C1 | 日付・列・データソースの選び分けと既定、データに由来する答え方の決まり（C4） |
| `Term` | C2 | 問いの言葉の解釈、部門や場面ごとの選択 |
| `Method` | C3 / C4 | 比べ方・分析の手順、出来事をまたぐ比べ方 |
| `Cross-source metric` | C3 | 1つの PDS の calc にできない、データソースをまたぐ指標 |

置かないもの:
- 列やデータソースが「何であるか」（説明に置く）。
- 集計のしかた（既定の集計に置く）。
- 1つの PDS の中で式にできる指標（calc に置く）。
- データに依らないエージェントの作法（グラフ・ツールの選び方、口調、出力形式）。これはスキルに置く。

## 文面の書式

- **1件1判断**。1つのデータソースの「既定の日付と、その例外」は1件にまとめてよい。
- `name` は `<kind>: <判断の短い名前>`（例 `Term: Fiscal periods for Finance questions`）。`name` は同期の照合キーなので、台帳の中で重複させない。
- `statement` は英語で1〜2文。命令形か定義文で書き、理由は書かない（理由は台帳の `rationale` に置く）。
- 問いの言葉（ユーザーが使う語）は二重引用符で囲む（例 `"this year"`）。名前の参照と区別するため。
- 説明（メタデータ）と同じ語を定義し直さない。説明が既に言っていることは BP に書かない。
- 配置は `isGlobal: true` を既定にする。1つのノードを grounding したときだけ効けばよい決まりに限り、`targetNodeId` を使う。

## 名前の書き方

データソース名とフィールド名は、**正確な名前をバッククォートで囲む**。参照切れを機械で点検するため。

| 対象 | 書き方 | 例 |
|---|---|---|
| フィールド | `` `フィールド名` in `データソース名` `` | `` `Revenue` in `rpt_acme__billing` `` |
| データソース | `` `データソース名` `` | `` `rpt_acme__sales_orders` `` |
| 名前の族（接頭辞で表す） | バッククォートの中で `*` を使う | `` `rpt_acme__*` `` |
| 同じ名前の列が複数ある | PDS 上の正確なキャプションを書く | `` `Shipped Date (shipment_lines.csv)` in `rpt_acme__shipments` `` |

- フィールドは必ず `in` でデータソースを修飾する。同じ文で同じフィールドを繰り返すときも省略しない。
- `*` の glob は、データソースを足すたびに BP を直さずに済ませるためのもの。点検では1件以上の一致を必須にする。
- `<table>` のような山括弧の可変部は使わない。`*` に置き換える。
- `{{NAME}}` のプレースホルダはセットアップ時に置換される前提で、点検の対象外。
- 同じ名前の列が複数あるとき、括弧なしの名前と括弧付きの名前のどちらを指すかを決めてから書く。列のリネームで解消できるが、下流を壊すので BP の側では正確なキャプションで指す。

## 台帳のスキーマ

台帳は BP の英文に、訳・理由・依存を添えた JSON。文面の正本は Knowledge で、台帳は利用者のリポジトリで git 管理する（steward の `work/` には置かない）。

```json
{
  "graph_id": null,
  "items": [
    {
      "kind": "Term",
      "name": "Term: Fiscal periods for Finance questions",
      "statement": "For Finance questions, \"this year\" means the fiscal year: group by `Fiscal Month` in `rpt_acme__billing`.",
      "translation": "Finance の問いでは「今年」は会計年度を指す。`Fiscal Month` でまとめる。",
      "rationale": "Finance は会計年度で締めるため（C2）",
      "scope": "global",
      "target_node_id": null,
      "depends_on": ["Fiscal Month in rpt_acme__billing"],
      "context_id": "CONTEXT_ID_PLACEHOLDER",
      "synced_statement": "For Finance questions, ..."
    }
  ]
}
```

| キー | 書く人 | 意味 |
|---|---|---|
| `kind` / `name` / `statement` | 人 / describer の `bp_candidates[]` | BP 本体。`name` が同期の照合キー |
| `translation` / `rationale` | 人 | 訳と理由。Knowledge には送らない |
| `scope` / `target_node_id` | 人 | `global`（既定）か `node` |
| `depends_on` | `refs --write-back` | statement から抜いた参照名。手で書かない |
| `context_id` / `synced_statement` | `plan --write-back` | 最後に Knowledge と一致を確認した文面。3方向比較の基準 |
| `translation_stale` | `plan --write-back` | Knowledge 側の編集を取り込んだので訳が古い |

セットアップ用の `title` / `text` 形式の JSON も、`name` / `statement` として読める。

## 点検結果の意味

`knowledge_ledger.py check` の `status`:

| status | 重さ | 意味と直し方 |
|---|---|---|
| `ok` | ok | 実在する |
| `missing_datasource` | error | データソースが無い（改名・削除）。新しい名前に直すか、BP を廃止する |
| `missing_field` | error | フィールドが無い。新しいキャプションに直す |
| `unresolved` | error | データソースでもなく、同じ文にデータソースの参照も無い名前。`in` で修飾する |
| `ambiguous_field` | warning | 同名の列（`X` と `X (...)`）がある。意図した方を正確な名前で書いたか確かめる |
| `unqualified_field` | warning | `in` の修飾が無い。修飾を足す |
| `duplicate_datasource_name` | warning | 同じ名前の PDS が複数プロジェクトにある。どれを指すかは名前だけでは決まらない |
| `placeholder_in_name` | warning | 山括弧の可変部。`*` の glob に直す |
| `unquoted_datasource` / `unquoted_field` | warning | 実在する名前がバッククォートの外に出ている。引用する |

`field_descriptions` は、参照先フィールドの現在の説明。二重定義の判定材料に使う。
