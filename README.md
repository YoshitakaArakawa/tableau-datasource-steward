# tableau-datasource-steward

Tableau Server / Tableau Cloud の **Published Data Source (PDS)** を、組織のセマンティックレイヤーとして継続メンテする Agent。列の説明・粒度（grain）・計算フィールドといったメタデータの整備を、Claude Code 上の Skill 群が Tableau API 経由で肩代わりする。

できることは大きく 2 つ。

- **メタデータ整備バッチ**: 説明の無い列・現データと合わなくなった既存説明を検出し、草案を生成して PDS に一括反映する。反映後はライブカタログを読み直した HTML レポートで「実際に何が入ったか」を網羅確認できる
- **計算フィールドの集約（hoist）**: 複数 workbook に重複するローカル計算フィールドを検出して PDS へ寄せ、説明も付けた上で、下流 workbook を書き換えて前後の view 描画を並置比較する

## 背景

Tableau の Published Data Source は、指標（計算フィールド）の定義、Dimension / Measure の区別、各列の意味（説明・デフォルト集計などのメタデータ）を保持する。実質的に組織のセマンティックレイヤーとして機能する。

> "A published data source is the closest equivalent Tableau has to a semantic layer or semantic model."
>
> — [Understanding Salesforce and Data Cloud Terms — Tableau Help](https://help.tableau.com/current/online/en-us/tableau_next_sf_datacloud_terms.htm)

しかし、このメタデータを人間が手作業で保守し続けるのは辛い。データソースが増え列が増えるほど破綻する。そこを API で自動化・支援するのが本リポジトリのゴール。

## ワークフロー

### メタデータ整備バッチ

対象範囲（project / PDS 名）を指定すると自走するバッチ。MCP / VDS が読む description を整備し、発見性を上げる。

1. **コスト見積もり**: `scripts/batch_augment.py estimate` で対象範囲の作業量（in-place で埋まる数 / republish が必要な数 / extract 総量）を先に算出し、重すぎる場合は絞り直しを促す
2. **棚卸しと草案生成**: 説明の無い列・stale な既存説明・grain 未記載を検出し、各説明に出所（抽出 / 推論）を付けた change-set を作る
3. **書き込み**: source 列の説明は Catalog に in-place 反映（PDS を作らない・触らない）。フィールド説明・grain はゲート付きの desc-only republish（`scripts/batch_augment.py run`、進捗 manifest 付きで再開可能）
4. **レビュー面生成**: `scripts/metadata_report.py` が反映後のライブカタログを読み直し、PDS ごとの grain + 全列 description + カバレッジ集計を自己完結 HTML 1 枚に出力する。書いた spec ではなく読み直した実物を確認できる

### 計算フィールドの集約（hoist）

下流 workbook に散らばった共通計算を PDS に集約し、説明まで一度に付けるワークフロー。

1. 下流 workbook を辿って重複計算フィールドを検出し、PDS へ寄せられるか（table calc や LOD の view 文脈依存がないか）を分類する
2. 各計算フィールドの説明を生成し、別名の draft PDS（`…__augmented`）に注入して publish する
3. 下流 workbook を rewired 版として publish し、書き換え前後の全 view を画像レンダリングして並置比較する
4. 結果レビューと承認を経て、本番 PDS / workbook へ反映する（承認ゲートはここ）

## 構成

中核ロジックは `.claude/skills/` 配下の 5 Skill に分かれ、ルーティングと横断ポリシーは orchestrator（[CLAUDE.md](CLAUDE.md)）が担う。データは「読取・提案（副作用なし）→ change-set → write」の一方向に流れる。write は PDS 向けと workbook 向けの 2 Skill のみ。

| Skill | 系統 | 役割 |
|---|---|---|
| `datasource-inspector` | READ | PDS の現状棚卸し（列・既存 calc・grain・説明カバレッジ） |
| `datasource-describer` | ANALYZE | 説明草案の生成と既存説明の検証（valid / stale） |
| `workbook-calc-prospector` | ANALYZE | 下流 workbook の重複 calc 検出 |
| `datasource-augmenter` | WRITE | change-set の注入・publish・round-trip 検証 |
| `workbook-rewirer` | WRITE | 下流 workbook の PDS 付け替え・calc 参照差し替え・view 描画検証 |

各 Skill の詳細な責務分担は [CLAUDE.md](CLAUDE.md) の Skill マップを正とする。

共通モジュールは `scripts/` に置く。

| ファイル | 役割 |
|---|---|
| `scripts/tableau_auth.py` | OAuth (PKCE) サインイン。`status` サブコマンドで接続前チェック |
| `scripts/metadata_api.py` | Metadata API (GraphQL) クライアント。読取の主経路 |
| `scripts/batch_augment.py` | 整備バッチの orchestrator（コスト見積もり / 一括実行・再開） |
| `scripts/metadata_report.py` | 反映結果のレビュー用 HTML レポート生成（読取専用） |

## 安全設計

書き込みは対象ごとに方式を分け、破壊的操作の前だけ承認ゲートを置く。

| 対象 | 方式 |
|---|---|
| source 列の説明 | Catalog in-place 更新（REST Update Column）。PDS を作らない・触らない。下流に `descriptionInherited` として継承される |
| PDS のフィールド説明・grain | desc-only republish。差分が説明のみであることを publish 前に XML 比較で証明し、LUID 維持・round-trip を事後検証する。ゲートを 1 つでも通らなければ publish しない |
| 計算フィールドの注入 | `CreateNew` で別名 draft を作る。本番反映（promote = Overwrite）は承認必須 |
| workbook の書き換え | 既定 `CreateNew` で rewired 版を作る。本番 workbook の Overwrite は承認必須 |

draft には `…__augmented` / `…__rewired` のような接尾辞を付け、正式版と誤認されないようにする。承認ゲートの範囲・自走時の報告義務など詳細は [CLAUDE.md](CLAUDE.md) の横断ポリシーを正とする。

## セットアップ

前提: Claude Code、Python 3、Tableau Server / Tableau Cloud のサイト。メタデータ整備（Catalog への書き込み・説明継承）には Data Management (Catalog) の有効化が必要。

```sh
pip install -r requirements.txt
cp .env.template .env   # SERVER / SITE_NAME を実値に差し替え（.env は gitignore 済み）
```

認証は OAuth 2.0 (Authorization Code + PKCE) のブラウザサインインで、静的トークンを持たない。`python scripts/tableau_auth.py status` で接続状態を確認できる。

あとはリポジトリを Claude Code で開き、「この project の列説明を整備して」のように依頼すれば、[CLAUDE.md](CLAUDE.md) が適切な Skill にルーティングする。

### Tableau MCP（読取系の一部で併用）

サンプル値取得などで Tableau MCP を使う。MCP サーバーの接続設定は利用者の Claude 環境側で用意する（connector / user スコープ等）。project スコープで設定したい場合の例として PAT/npx 版 `.mcp.json.template` を同梱している（`.mcp.json` にコピーして実値を差し替え。`.mcp.json` は gitignore 済み）。

Skill は datasource 読取ツール（`get-datasource-metadata` / `query-datasource` / `list-datasources` 等）を提供する MCP サーバーが利用可能であることだけを前提にし、ツールを基底名で参照するためサーバー名には依存しない。

## スコープと命名

対象は Tableau Server / Tableau Cloud の Published Data Source。古典的な Tableau Data Model（logical / physical layer を持つ `.tds` / `.tdsx` 系）が射程。

Tableau Next / Data 360 の「Tableau Semantics」「Semantic Model」は対象外。これは Salesforce のブランド製品名であり、別系統の API・オブジェクトモデルを持つ。リポジトリ名に semantic ではなく **datasource-steward**（datasource を継続して手入れする番人）を使うのは、このブランド衝突を避け、対象が Published Data Source だと一目で分かるようにするため。

## ライセンス

MIT
