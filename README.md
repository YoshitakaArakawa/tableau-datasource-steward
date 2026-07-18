# tableau-datasource-steward

Tableau Server / Tableau Cloud 上の **Published Datasource** を「セマンティックレイヤー」として継続メンテナンスする作業を、Tableau の API 経由で代行・支援する Agent。配布形態は Agent パッケージだが、中核ロジックは Skill に固める。

## 背景

Tableau の Published Datasource は、実質的に組織のセマンティックレイヤーとして機能する。次を保持するためだ:

- 指標（計算フィールド）の定義
- Dimension / Measure の区別
- 各列の意味（説明・コメント・フォルダ・デフォルト集計などのメタデータ）

これは Tableau 公式ヘルプの記載でも裏付けられる。原文（一字一句）:

> "A published data source is the closest equivalent Tableau has to a semantic layer or semantic model."
>
> — [Understanding Salesforce and Data Cloud Terms — Tableau Help](https://help.tableau.com/current/online/en-us/tableau_next_sf_datacloud_terms.htm)

この一文は Tableau Next / Data Cloud の用語解説ページにある。「Tableau Next の Semantic Model に対応する、従来 Tableau 側で最も近いもの = Published Datasource」という対比の文脈で書かれている。本リポジトリのスコープ確定の論拠そのものである。

しかし、メタデータ整備や計算フィールド追加を人間が手作業で保守し続けるのは辛い。データソースが増え列が増えるほど破綻する。そこを API で自動化・支援する Agent を作るのが本リポジトリのゴール。

## スコープ

**対象**: Tableau Server / Tableau Cloud の Published Datasource。古典的な Tableau Data Model（logical / physical layer を持つ `.tds` / `.tdsx` 系）が射程。

**対象外**: Tableau Next / Data 360 の「Tableau Semantics」「Semantic Model」。これは Salesforce のブランド製品名であり、別系統の API・オブジェクトモデルを持つ。

## 機能の核

- 既存 Published Datasource の**メタデータを埋める／整える**: 列の説明、デフォルト集計、フォルダ構成、Dimension / Measure 整理など（デフォルト集計・フォルダ構成の反映は将来拡張）
- **計算フィールドを作成・追加する**（指標定義をコードとして管理する発想）
- それらを**再パブリッシュして反映する**

全体として「人間が辛い保守を Agent が肩代わりする」体験を作る。

## 命名

Repo 名 = Agent 名を `tableau-datasource-steward` で統一する。

- **steward**（継続して手入れする番人）= 自動メンテの主体性を表す
- **datasource** を明示 = 対象が Server / Cloud の Published Datasource だと一目で分かり、Tableau Next の Semantic Model と誤読されない
- **semantic** は不採用（前述のブランド衝突）。思想は本文の説明で表現する

## 設計方針

- Skill の作成・設計時は、着手前に必ず `/creating-skills` を参照する
- 本リポジトリは将来 Public 化されうる前提で作る:
  - 絶対パス・個人情報・実データを本文／コミット／サンプルに書かない
  - 例示はダミー値にする
  - LICENSE は MIT を既定で置く

## 構成

中核ロジックは `.claude/skills/` 配下の 4 Skill に分かれ、ルーティングと横断ポリシーは orchestrator（[CLAUDE.md](CLAUDE.md)）が担う。データは「読取・提案（副作用なし）→ change-set → write（唯一の出口）」の一方向に流れる。

| Skill | 系統 | 役割 |
|---|---|---|
| `datasource-inspector` | READ | PDS の現状棚卸し |
| `datasource-describer` | ANALYZE | 説明草案の生成と既存説明の検証 |
| `workbook-calc-prospector` | ANALYZE | 下流 workbook の重複 calc 検出 |
| `datasource-augmenter` | WRITE | change-set の注入・publish・round-trip 検証 |

各 Skill の詳細な責務分担は [CLAUDE.md](CLAUDE.md) の Skill マップを正とする。

共通モジュールは `scripts/`（OAuth サインインの `tableau_auth.py`、Metadata API クライアントの `metadata_api.py`）。

## 前提（Tableau MCP）

読取系の一部で Tableau MCP を使う。**MCP サーバーの接続設定は利用者の Claude 環境側で用意する**（connector / user スコープ等）。本 repo は特定の接続を強制しないが、project スコープで設定したい場合の例として PAT/npx 版 `.mcp.json.template` を同梱する（`.mcp.json` にコピーし実値を差し替え。`.mcp.json` は gitignore 済み）。

Skill は datasource 読取ツール（`get-datasource-metadata` / `query-datasource` / `list-datasources` 等）を提供する MCP サーバーが利用可能であることだけを前提にし、ツールは tool 名（基底名）で参照するのでサーバー名には依存しない。

REST / Metadata API 用の認証（`scripts/`）は MCP とは別系統で、`.env`（`.env.template` を元に作成、gitignore 済み）で管理する。

## 安全ポリシー

- **publish は既定 `CreateNew`**: 別名で新規 PDS を作り、元を壊さない。`Overwrite` は破壊的（下流 workbook を巻き込みうる）で、明示要求 + 下流影響の提示 + ユーザー承認がそろったときのみ。
- **承認ゲートは破壊的操作の前**: Overwrite publish / PDS 削除 / promote の前は必ず確認。読取・change-set 生成・CreateNew は非破壊。
- **認証は OAuth**（PKCE）。読取系の一部は Tableau MCP を併用。

承認ゲートの範囲・自走ユースケースでの扱いなど詳細は [CLAUDE.md](CLAUDE.md) の横断ポリシーを正とする。

## ライセンス

MIT
