"""Read existing calculated fields (and their descriptions) plus the datasource
description (grain) of a Published Data Source via the Metadata API. MCP
get-datasource-metadata lists only physical columns and does not surface the
datasource-level description, so both must be read here.

usage:
    python read_calcs.py --pds-luid <luid> [--out calcs.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


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
    name description
    fields{
      name __typename description
      ... on CalculatedField{ formula }
    }
  }
}
"""


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
    calcs = [{"name": f["name"], "formula": f.get("formula"),
              "description": f.get("description")}
             for f in ds["fields"] if f["__typename"] == "CalculatedField"]
    out = {"pds_name": ds["name"],
           "datasource_description": ds.get("description"),  # grain (may be None/empty)
           "calc_count": len(calcs), "calcs": calcs}
    if args.out:
        Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print("RESULT_JSON:", json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
