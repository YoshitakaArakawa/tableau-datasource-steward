# tableau-datasource-steward

Tableau Server / Tableau Cloud の **Published Data Source (PDS)** を、組織のセマンティックレイヤーとして継続メンテナンスする Agent です。列の説明・粒度（grain）・計算フィールドといったメタデータの整備を、Claude Code 上の Skill 群が Tableau API 経由で肩代わりします。

できることは大きく 2 つです。

- **メタデータ整備バッチ**: 説明の無い列や、現データと合わなくなった既存説明を検出し、草案を生成して PDS に一括反映する。反映後は、ライブカタログを読み直した HTML レポートで「実際に何が入ったか」を網羅確認できる
- **計算フィールドの集約（hoist）**: 複数 workbook に重複するローカル計算フィールドを検出して PDS へ寄せ、説明も付けた上で、下流 workbook を書き換えて前後の view 描画を並置比較する

## 背景

Tableau の Published Data Source は、実質的に組織のセマンティックレイヤーとして機能します。次を保持するためです。

- 指標（計算フィールド）の定義
- Dimension / Measure の区別
- 各列の意味（説明・デフォルト集計などのメタデータ）

> "A published data source is the closest equivalent Tableau has to a semantic layer or semantic model."
>
> — [Understanding Salesforce and Data Cloud Terms — Tableau Help](https://help.tableau.com/current/online/en-us/tableau_next_sf_datacloud_terms.htm)

しかし、このメタデータを人間が手作業で保守し続けるのは大変です。データソースが増え、列が増えるほど破綻します。そこを API で自動化・支援するのが本リポジトリのゴールです。

## ワークフロー

### メタデータ整備バッチ

対象範囲（project / PDS 名）を指定すると自走するバッチです。検索や AI エージェント（MCP / VizQL Data Service）が参照する description を整備し、データソースの発見性を上げます。

1. **コスト見積もり**: `scripts/batch_augment.py estimate` が対象範囲の作業量（in-place で埋まる数 / republish が必要な数 / extract 総量）を先に算出します。重すぎる場合は対象の絞り直しを促します
2. **棚卸しと草案生成**: 説明の無い列、stale な既存説明、grain 未記載を検出し、各説明に出所（抽出 / 推論）を付けた change-set を作ります
3. **書き込み**: source 列の説明は Catalog に in-place で反映します（PDS を作らない・触らない）。フィールド説明・grain はゲート付きの desc-only republish で反映します（`scripts/batch_augment.py run`。進捗 manifest 付きで再開可能）
4. **レビュー面生成**: `scripts/metadata_report.py` が反映後のライブカタログを読み直し、PDS ごとの grain + 全列 description + カバレッジ集計を自己完結 HTML 1 枚に出力します。書いた spec ではなく、読み直した実物を確認できます

### 計算フィールドの集約（hoist）

下流 workbook に散らばった共通計算を PDS に集約し、説明まで一度に付けるワークフローです。

1. 下流 workbook を辿って重複計算フィールドを検出し、PDS へ寄せられるか（table calc や LOD の view 文脈依存が無いか）を分類します
2. 各計算フィールドの説明を生成し、別名の draft PDS（`…__augmented`）に注入して publish します
3. 下流 workbook を rewired 版として publish し、書き換え前後の全 view を画像レンダリングして並置比較します
4. 結果レビューと承認を経て、本番 PDS / workbook へ反映します（承認ゲートはここです）

## 構成

中核ロジックは `.claude/skills/` 配下の 5 Skill に分かれ、ルーティングと横断ポリシーは orchestrator（[CLAUDE.md](CLAUDE.md)）が担います。データは「読取・提案（副作用なし）→ change-set → write」の一方向に流れます。write は PDS 向けと workbook 向けの 2 Skill だけです。

| Skill | 系統 | 役割 |
|---|---|---|
| `datasource-inspector` | READ | PDS の現状棚卸し（列・既存 calc・grain・説明カバレッジ） |
| `datasource-describer` | ANALYZE | 説明草案の生成と既存説明の検証（valid / stale） |
| `workbook-calc-prospector` | ANALYZE | 下流 workbook の重複 calc 検出 |
| `datasource-augmenter` | WRITE | change-set の注入・publish・round-trip 検証 |
| `workbook-rewirer` | WRITE | 下流 workbook の PDS 付け替え・calc 参照差し替え・view 描画検証 |

各 Skill の詳細な責務分担は [CLAUDE.md](CLAUDE.md) の Skill マップを正とします。

共通モジュールは `scripts/` に置いています。

| ファイル | 役割 |
|---|---|
| `scripts/tableau_auth.py` | OAuth (PKCE) サインイン。`status` サブコマンドで接続前チェック |
| `scripts/metadata_api.py` | Metadata API (GraphQL) クライアント。読取の主経路 |
| `scripts/batch_augment.py` | 整備バッチの orchestrator（コスト見積もり / 一括実行・再開） |
| `scripts/metadata_report.py` | 反映結果のレビュー用 HTML レポート生成（読取専用） |

## 安全設計

書き込みは対象ごとに方式を分け、破壊的操作の前だけ承認ゲートを置きます。

| 対象 | 方式 |
|---|---|
| source 列の説明 | Catalog in-place 更新（REST Update Column）。PDS を作らない・触らない。下流に `descriptionInherited` として継承される |
| PDS のフィールド説明・grain | desc-only republish。差分が説明のみであることを publish 前に XML 比較で証明し、publish 後は ID (LUID) の維持と説明の読み戻し一致（round-trip）を検証。ゲートを 1 つでも通らなければ publish しない |
| 計算フィールドの注入 | `CreateNew` で別名 draft を作る。本番反映（promote = Overwrite）は承認必須 |
| workbook の書き換え | 既定 `CreateNew` で rewired 版を作る。本番 workbook の Overwrite は承認必須 |

draft には `…__augmented` / `…__rewired` のような接尾辞を付け、正式版と誤認されないようにします。承認ゲートの範囲や自走時の報告義務など、詳細は [CLAUDE.md](CLAUDE.md) の横断ポリシーを正とします。

## セットアップ

前提は Claude Code、Python 3、Tableau Server / Tableau Cloud のサイトです。メタデータ整備（Catalog への書き込み・説明継承）には Data Management (Catalog) の有効化が必要です。

```sh
pip install -r requirements.txt
cp .env.template .env   # SERVER / SITE_NAME を実値に差し替え（.env は gitignore 済み）
```

認証は OAuth 2.0 (Authorization Code + PKCE) のブラウザサインインで、静的トークンを持ちません。`python scripts/tableau_auth.py status` で接続状態を確認できます。

あとはリポジトリを Claude Code で開き、「この project の列説明を整備して」のように依頼すれば、[CLAUDE.md](CLAUDE.md) が適切な Skill にルーティングします。

### Tableau MCP（読取系の一部で併用）

サンプル値の取得などで Tableau MCP を使います。MCP サーバーの接続設定は、利用者の Claude 環境側で用意してください（connector / user スコープ等）。project スコープで設定したい場合の例として、PAT/npx 版の `.mcp.json.template` を同梱しています（`.mcp.json` にコピーして実値を差し替え。`.mcp.json` は gitignore 済み）。

Skill 側が前提にするのは、datasource 読取ツール（`get-datasource-metadata` / `query-datasource` / `list-datasources` 等）を提供する MCP サーバーが利用可能であることだけです。ツールは基底名で参照するため、サーバー名には依存しません。

## スコープと命名

対象は Tableau Server / Tableau Cloud の Published Data Source です。古典的な Tableau Data Model（logical / physical layer を持つ `.tds` / `.tdsx` 系）が射程です。

Tableau Next / Data 360 の「Tableau Semantics」「Semantic Model」は対象外です。これは Salesforce のブランド製品名で、別系統の API・オブジェクトモデルを持ちます。リポジトリ名に semantic を使わず **datasource-steward**（datasource を継続して手入れする番人）としたのは、このブランド衝突を避けるためです。対象が Published Data Source であることが、名前だけで分かるようにする意図もあります。

## ライセンス

MIT
