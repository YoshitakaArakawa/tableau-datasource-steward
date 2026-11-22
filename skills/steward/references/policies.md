---
purpose: steward の横断ポリシー（メタデータとコンテキストの分離、書き込み方式と承認、認証と MCP の前提）
note: 承認ゲートの要点は SKILL.md の「最優先の規範」、置き場所の判定表は datasource-describer の references/placement-rules.md を正とする。この文書はその背景と対応表を持つ
---

# 横断ポリシー

## 目次
- メタデータとコンテキストを分ける
- 書き込み方式と承認
- 認証と MCP

## メタデータとコンテキストを分ける

| 層 | 答える問い | 置き場 |
|---|---|---|
| メタデータ層 | それは何か | PDS の構造（calc・名前・関係）と説明（列・calc・grain） |
| コンテキスト層 | どう使うか | 組織で共有する決まりは Tableau Knowledge の Business Preference（BP）。データに依らないエージェントの作法はスキル |

1. **判定**: その文は、問う人・問い・場面が変わっても真か。真ならメタデータ、変わりうるならコンテキスト。
2. **依存は一方向**: コンテキストはメタデータを名前で参照してよい。メタデータはコンテキストを参照しない。
3. **一か所で定義する**: 同じ語を両方の層で定義しない。

置き場所の優先順は **構造 → 説明 → 例 → 文の指示（BP）**。この規範は Tableau 公式（説明に典型的な使い方も書く）より厳しい。説明だけを読むエージェントに使い方が届かない代償は、構造を先にすることで小さくする。既定の集計は扱わない（VizQL Data Service のクエリでは集計関数をクライアントが選ぶ）。加算できない measure は、その性質を説明に書く。

## 書き込み方式と承認

前提: Data Management（Catalog）が有効なサイト。

| 対象 | 方式 | 承認 |
|---|---|---|
| source 列の説明 | Catalog in-place（REST Update Column）。PDS を作らない・触らない。全下流に `descriptionInherited` として継承 | 自走可（バッチ宣言に含める）。rollback は元値の逆適用 |
| PDS の field desc・grain | **desc-only Overwrite**（`calcs` を含まない spec のみ。preflight + XML diff ゲート + LUID 検証を通過したときだけ publish） | **バッチ宣言 1 回**で自走可。per-PDS の事前確認は置かない。rollback は `--rollback` |
| calc 注入 | `CreateNew` で別名 draft（例 `…__augmented`）を作る | draft 作成は自走可。本番反映（promote）は承認必須 |
| 内容変更を伴う Overwrite（promote 含む） | 破壊的（下流 workbook を巻き込みうる） | **明示要求 + 影響の提示 + ユーザー承認** |
| workbook（rewirer） | 既定 `CreateNew`（rewired 版）。本番 WB の Overwrite は破壊的 | draft 作成は自走可。本番 Overwrite は承認必須 |
| Knowledge の BP | Tableau MCP `manage-knowledge-context` のみ。draft は無く、書いた時点で全員に効く | create / update はバッチ宣言 1 回で自走可。ただし点検（参照切れ）で error / unknown が 0 件の項目に限る（機械ゲート）。書き込み前のスナップショットを取り消しの基準として保存し、事後報告に contextId と取り消し手順を載せる。delete は対象を示して承認必須 |

desc-only Overwrite が準非破壊と言えるのは、人間の目視承認を機械証明に置き換えているため。差分が `<desc>` に限られることを publish 前に XML 比較で証明し、LUID 維持・grain 継承・round-trip を事後検証する。ゲートを 1 つでも通らなければ publish しない。

削除は `skills/datasource-augmenter/scripts/cleanup_drafts.py`（name 接尾辞 ∧ project の二重ガード、dry-run 既定）を標準手段とする。自走時も draft と分かる命名（例 `…__augmented`、`…__rewired`）を使い、第三者が正式版と誤認しないようにする。

## 認証と MCP

- Tableau API は **OAuth**（`scripts/tableau_auth.py` の `signed_in_server()`）で叩く。`.env` は呼び出し側の作業ディレクトリから親へたどって探す。セッションのキャッシュはユーザー単位のディレクトリ（Windows は `%LOCALAPPDATA%/tableau-datasource-steward`、それ以外は `~/.config/tableau-datasource-steward`、`STEWARD_AUTH_CACHE_DIR` で上書き可）に置く。
- 読取の主経路は Metadata API GraphQL（`scripts/metadata_api.py`）。列メタ・既存 calc・grain・lineage を一括で安定して読める。
- Tableau MCP はサンプル値取得（`query-datasource`）と、GraphQL に無い属性の補完に使う。並列呼び出しで断続 401 を返すため、バッチの主経路にしない。
- **Knowledge の読み書きは Tableau MCP だけで行う**（`inspect-knowledge-context` / `manage-knowledge-context` / `query-knowledge-context`。Tableau+ が要る）。
- MCP サーバーの用意（接続方式・認証）は利用者の Claude 環境側の責務。datasource 読取ツール（`get-datasource-metadata` / `query-datasource` / `list-datasources` 等）を提供する MCP サーバーが利用可能であることを前提にする。ツールはサーバー名 prefix ではなく tool 名（基底名）で参照する。
