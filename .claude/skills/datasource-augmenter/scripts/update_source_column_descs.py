"""Source 列（REST addressable な実テーブルの列）の Catalog description を in-place 更新する。

PDS フィールドの description（.tds 埋め込み。republish が必要）とは別スロット。
ここで書いた説明は lineage を通じて**全下流 PDS** に継承され、`get-datasource-metadata`
では `descriptionInherited` 属性として露出する。PDS を作らず・壊さず書ける経路。

制約:
- 書けるのは REST の tables エンドポイントが認識する実ソース列のみ。
  hyper extract の内部テーブル（Prep 出力 PDS の直上流 "Extract"）は対象外。
- 継承が意味を保つのは PDS フィールドと source 列が 1:1 のときだけ。
  複数 upstream 列を持つ派生フィールドは resolve が ineligible に落とす。
- 反映は Catalog インデックス経由で遅延する（目安 15〜60 秒）。verify は REST 直読で行う。

usage:
    python update_source_column_descs.py resolve --pds-luid <luid> --out resolve.json
    python update_source_column_descs.py apply --spec spec.json --out-dir <dir>
    python update_source_column_descs.py rollback --result <dir>/result.json

apply の spec 形式（resolve の candidates から組み立てる）:
    {"updates": [{"table_luid": "...", "column_luid": "...", "description": "...",
                  "field": "任意（報告用の PDS フィールド名）"}]}

Auth: OAuth (scripts/tableau_auth.py, signed_in_server())
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.sax.saxutils as sx
from pathlib import Path

# Windows コンソール (cp932) の文字化け対策。列名照合はファイル経由が正。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


def _find_repo_scripts() -> Path:
    cur = Path(__file__).resolve()
    for _ in range(8):
        cur = cur.parent
        cand = cur / "scripts" / "tableau_auth.py"
        if cand.exists():
            return cand.parent
    raise RuntimeError("repo-root scripts/ (with tableau_auth.py) not found")


sys.path.insert(0, str(_find_repo_scripts()))
import requests  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402
from metadata_api import graphql  # noqa: E402


def _rest(server, method: str, path: str, **kw):
    url = f"{server.server_address}/api/{server.version}/sites/{server.site_id}/{path}"
    headers = {"X-Tableau-Auth": server.auth_token, "Accept": "application/json"}
    headers.update(kw.pop("headers", {}))
    resp = requests.request(method, url, headers=headers, timeout=30, **kw)
    return resp


def _read_column(server, table_luid: str, column_luid: str) -> dict | None:
    """REST 直読で列の現在値を返す（Catalog インデックス遅延の影響を受けない）。"""
    r = _rest(server, "GET", f"tables/{table_luid}/columns", params={"pageSize": 1000})
    if r.status_code != 200:
        raise RuntimeError(f"read columns HTTP {r.status_code}: {r.text[:200]}")
    for c in (r.json().get("columns", {}) or {}).get("column", []):
        if c["id"] == column_luid:
            return c
    return None


def _write_column(server, table_luid: str, column_luid: str, description: str) -> int:
    body = ("<tsRequest><column description="
            f"\"{sx.escape(description, {chr(34): '&quot;'})}\"/></tsRequest>")
    r = _rest(server, "PUT", f"tables/{table_luid}/columns/{column_luid}",
              data=body.encode("utf-8"), headers={"Content-Type": "application/xml"})
    return r.status_code


# --- resolve -------------------------------------------------------------------
FIELD_QUERY = """
query ($l: String!) {
  publishedDatasources(filter: {luid: $l}) {
    name luid
    fields {
      __typename name description
      ... on ColumnField {
        upstreamColumns {
          name luid description
          table { __typename name ... on DatabaseTable { luid } }
        }
      }
    }
  }
}
"""


def cmd_resolve(server, pds_luid: str, out_path: Path) -> None:
    pubs = graphql(server, FIELD_QUERY, {"l": pds_luid}).get("publishedDatasources") or []
    if not pubs:
        raise SystemExit(f"PDS not found in Metadata API: {pds_luid}")
    ds = pubs[0]

    rest_tables = (_rest(server, "GET", "tables", params={"pageSize": 1000})
                   .json().get("tables", {}) or {}).get("table", [])
    addressable = {t["id"] for t in rest_tables}

    candidates, ineligible = [], []
    for f in ds["fields"]:
        if f["__typename"] != "ColumnField":
            ineligible.append({"field": f["name"], "reason": f["__typename"]
                               + " は source 列を持たない（republish 経路へ）"})
            continue
        ups = f.get("upstreamColumns") or []
        if len(ups) != 1:
            ineligible.append({"field": f["name"],
                               "reason": f"upstream 列が {len(ups)} 件（1:1 でない）"})
            continue
        col = ups[0]
        t = col.get("table") or {}
        t_luid = t.get("luid")
        if not t_luid or t_luid not in addressable:
            ineligible.append({"field": f["name"],
                               "reason": f"table {t.get('name')!r} は REST 非対応"
                                         "（hyper extract 等）"})
            continue
        candidates.append({
            "field": f["name"],
            "field_description": f.get("description"),
            "column_name": col["name"], "column_luid": col["luid"],
            "table_name": t["name"], "table_luid": t_luid,
            "current_source_description": col.get("description") or "",
        })

    out = {"pds_name": ds["name"], "pds_luid": ds["luid"],
           "n_candidates": len(candidates), "n_ineligible": len(ineligible),
           "candidates": candidates, "ineligible": ineligible}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"RESOLVE: candidates={len(candidates)} ineligible={len(ineligible)} -> {out_path}")


# --- apply / rollback ----------------------------------------------------------
def _apply_updates(server, updates: list[dict], out_dir: Path, phase: str) -> dict:
    results = []
    for u in updates:
        prev = _read_column(server, u["table_luid"], u["column_luid"])
        if prev is None:
            results.append({**u, "status": "not_found", "verified": False})
            continue
        status = _write_column(server, u["table_luid"], u["column_luid"], u["description"])
        now = _read_column(server, u["table_luid"], u["column_luid"]) or {}
        results.append({
            "field": u.get("field"),
            "table_luid": u["table_luid"], "column_luid": u["column_luid"],
            "column_name": prev.get("name"),
            "previous": prev.get("description") or "",
            "written": u["description"],
            "http_status": status,
            "verified": (now.get("description") or "") == u["description"],
        })
    result = {
        "phase": phase,
        "n": len(results),
        "n_verified": sum(1 for r in results if r.get("verified")),
        "updates": results,
        "verified": all(r.get("verified") for r in results),
        "_note": ("Catalog/MCP (descriptionInherited) への露出はインデックス反映後"
                  "（目安 15〜60 秒）。verify は REST 直読で確定済み"),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / ("result.json" if phase == "apply" else "rollback_result.json")
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("RESULT_JSON:", json.dumps({k: result[k] for k in
                                      ("phase", "n", "n_verified", "verified")},
                                     ensure_ascii=False))
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_res = sub.add_parser("resolve", help="PDS フィールド→source 列の 1:1 対応を解決")
    p_res.add_argument("--pds-luid", required=True)
    p_res.add_argument("--out", required=True)
    p_app = sub.add_parser("apply", help="spec の updates[] を反映（元値を記録）")
    p_app.add_argument("--spec", required=True)
    p_app.add_argument("--out-dir", required=True)
    p_rb = sub.add_parser("rollback", help="apply の result.json から元値へ逆適用")
    p_rb.add_argument("--result", required=True)
    args = ap.parse_args()

    with signed_in_server() as server:
        if args.cmd == "resolve":
            cmd_resolve(server, args.pds_luid, Path(args.out))
        elif args.cmd == "apply":
            spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
            _apply_updates(server, spec["updates"], Path(args.out_dir), "apply")
        elif args.cmd == "rollback":
            prev = json.loads(Path(args.result).read_text(encoding="utf-8"))
            updates = [{"table_luid": r["table_luid"], "column_luid": r["column_luid"],
                        "description": r["previous"], "field": r.get("field")}
                       for r in prev["updates"] if r.get("http_status") == 200]
            _apply_updates(server, updates, Path(args.result).parent, "rollback")


if __name__ == "__main__":
    main()
