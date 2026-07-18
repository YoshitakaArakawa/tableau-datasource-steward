"""PDS のスキーマ棚卸しを Metadata API (GraphQL) 1 クエリで取得する。

列（name / dataType / role / isHidden / description / upstream 1:1）・既存 calc
（formula / description）・datasource description（grain）・擬似列の除外・skip 候補
の分類までを一括で行う。MCP get-datasource-metadata は並列呼び出しで不安定
（断続 401）なため、列メタもここに寄せ、MCP は defaultAggregation とサンプル値の
補完に限定する。

usage:
    python read_schema.py --pds-luid <luid> [--out schema.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Windows コンソール (cp932) の文字化け対策。列名照合はファイル経由が正。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def _find_repo_scripts() -> Path:
    cur = Path(__file__).resolve()
    for _ in range(8):
        cur = cur.parent
        if (cur / "scripts" / "tableau_auth.py").exists():
            return cur / "scripts"
    raise RuntimeError("repo-root scripts/ (with tableau_auth.py) not found")


sys.path.insert(0, str(_find_repo_scripts()))
from tableau_auth import signed_in_server  # noqa: E402  (OAuth)
from metadata_api import graphql  # noqa: E402

Q = """
query($l:String!){
  publishedDatasources(filter:{luid:$l}){
    name luid description
    upstreamTables{ name }
    fields{
      name __typename description isHidden
      ... on ColumnField{
        dataType role
        upstreamColumns{ luid table{ __typename name ... on DatabaseTable { luid } } }
      }
      ... on CalculatedField{ formula dataType role }
    }
  }
}
"""

# 定数だけの formula（"1", "1.0" 等）。`Number of Records` 相当の legacy calc の目印
_CONST_FORMULA = re.compile(r"^\s*-?\d+(\.\d+)?\s*$")
# 単一フィールド参照だけの formula（"[x]"）。Prep 由来の単純エイリアス calc の目印
_ALIAS_FORMULA = re.compile(r"^\s*\[[^\]]+\]\s*$")


def classify_skip(field: dict) -> str | None:
    """説明対象から外す候補の分類。判定はしない（理由付きでフラグするだけ）。"""
    if field.get("isHidden"):
        return "hidden フィールド（consumer に見えない）"
    formula = field.get("formula")
    if formula is not None:
        if _CONST_FORMULA.match(formula):
            return f"定数 calc（formula={formula.strip()!r}。Number of Records 相当の legacy）"
        if _ALIAS_FORMULA.match(formula):
            return f"単純エイリアス calc（formula={formula.strip()!r}。派生元の列で説明すべき）"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pds-luid", required=True)
    ap.add_argument("--out")
    args = ap.parse_args()

    with signed_in_server() as server:
        res = graphql(server, Q, {"l": args.pds_luid})["publishedDatasources"]
    if not res:
        raise SystemExit(f"PDS not found: {args.pds_luid}")
    ds = res[0]
    table_names = {t["name"] for t in ds.get("upstreamTables") or []}

    columns, calcs, pseudo, skip_candidates = [], [], [], []
    for f in ds["fields"]:
        # GraphQL は論理テーブル自体を ColumnField として数える（名前がテーブル名と
        # 一致し upstream 列を持たない）。実列ではないので除外リストへ。
        ups = f.get("upstreamColumns")
        if (f["__typename"] == "ColumnField" and f["name"] in table_names and not ups):
            pseudo.append(f["name"])
            continue
        skip = classify_skip(f)
        if skip:
            skip_candidates.append({"name": f["name"], "type": f["__typename"],
                                    "reason": skip})
        entry = {"name": f["name"], "dataType": f.get("dataType"),
                 "role": f.get("role"), "isHidden": f.get("isHidden"),
                 "description": f.get("description") or None}
        if f["__typename"] == "CalculatedField":
            calcs.append({**entry, "formula": f.get("formula")})
        elif f["__typename"] == "ColumnField":
            one = (ups or [None])[0] if len(ups or []) == 1 else None
            t = (one or {}).get("table") or {}
            columns.append({**entry, "upstream_1to1": bool(one),
                            "upstream_table": t.get("name")})
        else:  # SetField / GroupField / HierarchyField / BinField 等
            columns.append({**entry, "field_type": f["__typename"]})

    skip_names = {s["name"] for s in skip_candidates}
    gap = {
        "grain_missing": not (ds.get("description") or "").strip(),
        "undescribed_columns": sorted(c["name"] for c in columns
                                      if not c["description"] and c["name"] not in skip_names),
        "undescribed_calcs": sorted(c["name"] for c in calcs
                                    if not c["description"] and c["name"] not in skip_names),
    }
    out = {
        "pds_name": ds["name"], "pds_luid": ds["luid"],
        "datasource_description": ds.get("description"),  # grain（None/空 = 未設定）
        "column_count": len(columns), "calc_count": len(calcs),
        "columns": columns, "calcs": calcs,
        "pseudo_table_fields_excluded": pseudo,
        "skip_candidates": skip_candidates,
        "gap": gap,
    }
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {k: out[k] for k in ("pds_name", "column_count", "calc_count")}
    summary["undescribed"] = (len(gap["undescribed_columns"]), len(gap["undescribed_calcs"]))
    summary["grain_missing"] = gap["grain_missing"]
    print("RESULT_JSON:", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
