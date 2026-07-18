# tableau-datasource-steward — オーケストレーション指針

このリポジトリは、Tableau Server / Tableau Cloud 上の **Published Data Source (PDS)** を
セマンティックレイヤーとして継続メンテする Agent。中核ロジックは `.claude/skills/` の複数 Skill に分かれ、
本ファイルが orchestrator（ルーティングと横断ポリシー）を担う。

## Skill マップ

| Skill | 系統 | 責務 |
|---|---|---|
| `datasource-inspector` | READ | PDS のスキーマ・メタデータ・既存 calc を GraphQL 1 クエリで棚卸しし、整備 gap を洗い出す（既存 desc 全文・grain・擬似列除外・skip 候補付き） |
| `datasource-describer` | ANALYZE | 列・calc・datasource（grain）の説明草案を作り、既存 desc・grain が現データに適切かも検証（valid/stale）。出所付きで change-set 化 |
| `workbook-calc-prospector` | ANALYZE | 下流 workbook の重複 calc を検出し hoist 候補（+ WB 側 desc・コメント込み formula）を出す |
| `datasource-augmenter` | WRITE | change-set を受けて PDS メタデータを書き込む。source 列 desc は Catalog in-place、field desc / grain は desc-only Overwrite（ゲート付き）、calc は CreateNew publish。いずれも機械検証まで |
| `workbook-rewirer` | WRITE | hoist 後の workbook を書き換え（PDS 付け替え + calc 参照差し替え）→ publish → view 描画検証 |

データの流れ: inspector / describer / prospector（読取・提案、副作用なし）→ change-set → **augmenter（PDS への write）** → **rewirer（workbook への write）**。write はこの 2 Skill のみ。

「何を触るときにどの Skill を使うか」は本ファイル（Skill マップとルーティング）を正とし、**Skill 本文には他 Skill への routing 参照を書かない**（スコープ除外は行き先なしで書く）。Skill 側に書いてよいのは入出力契約まで（spec のフィールドがどの Skill の成果物由来か、実行手順上どの Skill の出力を前提にするか）。

## ルーティング

- 「現状を見たい / どの列に説明が無いか」→ `datasource-inspector`
- 「列の説明を埋めたい / データ辞書 / grain（粒度）を書きたい」→ `datasource-describer`（必要なら先に inspector）
- 「既存の説明が現データに合っているか点検したい」→ `datasource-describer`（検証モード。inspector が既存 desc・grain を渡す）
- 「共通の計算を PDS に寄せたい / 重複 calc を探す」→ `workbook-calc-prospector`
- 「説明 / calc を実際に PDS へ反映したい」→ `datasource-augmenter`
- 「寄せた calc を workbook で実際に使わせたい / workbook のデータソースを付け替えたい / rewired 版で動作確認したい」→ `workbook-rewirer`

Skill の作成・変更時は、着手前に `creating-skills` を参照する。

## ワークフロー：列 desc を一括整備する（メタデータ整備バッチ）

ユーザーが対象範囲（project / PDS 名）を指定して回す自走バッチ。発見性（MCP / VDS が読む description）の整備が目的。

1. **コスト見積もりゲート**: `scripts/batch_augment.py estimate` で per-PDS の「source 列経路で埋まる数 / republish 必至の数 / extract 総量」を算出。`heavy` なら「全部やると大変」を提示して対象の絞り直しを促す
2. `datasource-inspector`（`read_schema.py`）→ `datasource-describer` で説明草案を作り、**source 列経路の updates** と **republish 経路の change-set** に振り分ける（1:1 対応は inspector の `upstream_1to1` と augmenter の `resolve` が判定）
3. 書き込み（**バッチ実行の宣言 1 回で自走。per-PDS の事前確認は置かない**）:
   - source 列 desc → `update_source_column_descs.py apply`（in-place。PDS を作らない・触らない）
   - field desc / grain → `batch_augment.py run`（desc-only Overwrite。preflight + diff ゲート + LUID 検証を各 PDS で機械適用、manifest で再開可能）
4. **orchestrator 報告**: 本文は**要約と判断点のみ**（verified 集計、gate_failed / abort の理由、`inferred` で確信度の低いもの、rollback 手順）。全 luid・全変更の一覧は `work/` にファイル出力し、本文に列挙しない。折りたたみ目的で HTML `<details>` を使わない（ターミナルの Markdown では描画されない）

## ワークフロー：重複 calc を寄せて説明も付ける

下流 WB の共通 calc を PDS に集約し、その calc の description まで一度に埋めるユースケース。CreateNew の draft 上で非破壊に検証できるため、**推論を含めて一気に埋め、成果物を後でレビュー**する自走型で回す。

1. `workbook-calc-prospector` → hoist 候補（`wb_descriptions[]` / コメント込み `formula` 付き）
2. `datasource-describer` → 各 calc の `description` を生成し、出所（`extracted` / `inferred`）と衝突（`conflict` / `variants`）を付与
3. `datasource-augmenter` → `CreateNew` で別名 PDS に注入・publish・round-trip 検証
4. `workbook-rewirer` → 各下流 WB を `CreateNew` で rewired 版として publish（augmented PDS へ付け替え + calc 参照差し替え）し、全 view の描画検証まで実行（**ここまで承認プロンプトなしで自走**）
5. **orchestrator 報告**（このワークフローの最後）でユーザー確認を提示：
   - 公開した PDS / rewired WB の `name` / `published_luid` / project と**破棄手順**（いずれも CreateNew なので破棄は 1 操作ずつ）
   - rewirer の `view_checks` の結果（エラーが出た view は原文エラー併記で先頭に）
   - 説明の**抽出 / 推論の区別**、`conflict: true` の calc は原文（`variants`）併記で先頭に並べる
   - confidence の低い `inferred` を上位に置き、追認（rubber-stamp）にならないよう確認対象を絞る

この報告で承認が得られたら、本番への反映に進む: PDS の promote（`Overwrite`）→ `workbook-rewirer` の掃除モード（本番 PDS を `pds.luid` に指定。repoint なしの calc 差し替え）で本番 WB を `Overwrite`。CreateNew はビットを守るが**ユーザーの注意は守らない**ため、報告の legibility がこのワークフローの実質的な安全装置。

## 横断ポリシー

### 書き込み方式（DM/Catalog 有効を前提とする）

| 対象 | 方式 | 承認 |
|---|---|---|
| source 列の説明 | Catalog in-place（REST Update Column）。PDS を作らない・触らない。全下流に `descriptionInherited` として継承 | 自走可（バッチ宣言に含める）。rollback は元値の逆適用 |
| PDS の field desc・grain | **desc-only Overwrite**（`calcs` を含まない spec のみ。preflight + XML diff ゲート + LUID 検証を通過したときだけ publish） | **バッチ宣言 1 回**で自走可。per-PDS の事前確認は置かない。rollback は `--rollback` |
| calc 注入 | `CreateNew` で別名 draft（例 `…__augmented`）を作る | draft 作成は自走可。本番反映（promote）は承認必須 |
| 内容変更を伴う Overwrite（promote 含む） | 破壊的（下流 workbook を巻き込みうる） | **明示要求 + 影響の提示 + ユーザー承認** |
| workbook（rewirer） | 既定 `CreateNew`（rewired 版）。本番 WB の Overwrite は破壊的 | draft 作成は自走可。本番 Overwrite は承認必須 |

desc-only Overwrite が準非破壊と言えるのは、人間の目視承認を機械証明に置き換えているため: 差分が `<desc>` に限られることを publish 前に XML 比較で証明し、LUID 維持・grain 継承・round-trip を事後検証する。ゲートを 1 つでも通らなければ publish しない。

### 承認ゲート
- 破壊的操作（**内容変更を伴う Overwrite publish（PDS / workbook）、PDS・workbook の削除、promote**）の前は必ずユーザー確認。真のゲートはここ。削除は `cleanup_drafts.py`（name 接尾辞 ∧ project の二重ガード、dry-run 既定）を標準手段とする。
- `CreateNew`・desc-only Overwrite・source 列 desc・読取・change-set 生成は非破壊〜準非破壊。自走ユースケース（上記ワークフロー）では**事前プロンプトなしで実行してよい**（desc-only Overwrite はバッチ実行の宣言 1 回を先に置く）。代わりに事後の orchestrator 報告で、変更対象（name / luid / project）と rollback / 破棄手順、抽出/推論の区別を必ず提示する（confirm は write の前ではなくワークフロー末尾に束ねる）。
- 自走時も draft と分かる命名（例 `…__augmented`、`…__rewired`）を使い、第三者が正式版と誤認しないようにする。

### 認証
- Skill から Tableau API を叩くときは **OAuth**（`scripts/tableau_auth.py` の `signed_in_server()`）を使う。
- 読取の主経路は Metadata API GraphQL（`scripts/metadata_api.py`）。列メタ・既存 calc・grain・lineage を一括で安定して読める。Tableau MCP はサンプル値取得（`query-datasource`）と defaultAggregation 等の補完に使う（並列呼び出しで断続 401 のためバッチの主経路にしない）。
- **MCP サーバーの用意（接続方式・認証）は利用者の Claude 環境側の責務**。repo は特定の接続を強制しない（project スコープで設定したい場合の例として PAT/npx 版 `.mcp.json.template` を同梱）。Skill は datasource 読取ツール（`get-datasource-metadata` / `query-datasource` / `list-datasources` 等）を提供する MCP サーバーが利用可能であることを前提にする。ツールは**サーバー名 prefix ではなく tool 名（基底名）で参照**し、環境側のサーバー名に依存しない。

### 公開前提の規範
- このリポジトリは公開されうる。実 URL / 実 ID / 個人情報 / トークンを本文・コミット・サンプルに書かない。
- 秘匿値は `.env`（gitignore 済み）に置き、配布用はテンプレート（`.env.template`）。
- サンプル値はダミー（`https://example.tableau.com`, `LUID_PLACEHOLDER` 等）。

## work/ ディレクトリ規約

セッション作業物（inspector / describer / prospector の出力 JSON、change-set / rewire spec、augmenter / rewirer の out-dir、batch の manifest・estimate、DL した .tdsx / .twbx）は `work/<yyyymmdd>_<tag>/` に集約する。git 追跡は [work/README.md](work/README.md) のみ（規約詳細もそこに置く）。リポ直下や `.claude/` 配下に作業物を撒かない。

## 共通モジュール（`scripts/`）

| ファイル | 役割 |
|---|---|
| `scripts/tableau_auth.py` | OAuth (PKCE) サインイン。`signed_in_server()`。`status` サブコマンドが機械前チェック（exit 0 = cached session alive） |
| `scripts/metadata_api.py` | Metadata API (GraphQL) client。`graphql(server, query, vars)` |
| `scripts/batch_augment.py` | メタデータ整備バッチの orchestrator。`estimate`（対象範囲のコスト見積もりゲート）/ `run`（spec-dir を進捗 manifest 付きで一括実行・再開可能） |

## 依存

`tableauserverclient` / `python-dotenv` / `requests`（`requirements.txt`）。
