"""Render every view of every downstream workbook of a PDS as fresh PNGs.

Post-promote sweep: after a PDS content change (Overwrite / promote), the
round-trip and LUID checks prove the PDS itself survived, but not that its
downstream workbooks still render. This script lists ALL workbooks downstream
of the PDS via the Metadata API (rewired or not) and force-renders each of
their views, so a field reference broken by the promote surfaces as an export
error. Strictly read-only: no download, no publish, no metadata write.

usage:
    python render_downstream.py --pds-luid <PDS LUID> --out-dir <dir>

Output: PNGs under <out-dir>/<workbook_slug>_<luid8>/, a full per-view record
in <out-dir>/result.json, and a `RESULT_JSON:` summary line (tally + errors)
on stdout. Auth: OAuth (scripts/tableau_auth.py, signed_in_server()).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# --- locate shared modules in the repo-root scripts/ directory -----------------
def _find_repo_scripts() -> Path:
    cur = Path(__file__).resolve()
    for _ in range(8):
        cur = cur.parent
        cand = cur / "scripts" / "tableau_auth.py"
        if cand.exists():
            return cand.parent
    raise RuntimeError("repo-root scripts/ (with tableau_auth.py) not found")


sys.path.insert(0, str(_find_repo_scripts()))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling rewire_workbook
from tableau_auth import signed_in_server  # noqa: E402  (OAuth)
from metadata_api import graphql  # noqa: E402
from rewire_workbook import export_view_images, _slug  # noqa: E402  (fresh render, view-level error capture)

# Unlike the prospector, no direct-proxy scoping here: a promote can break any
# workbook that reaches this PDS, including ones connected via derived PDSes,
# so the transitive downstream set is exactly the sweep target.
DOWNSTREAM = """
query($l:String!){
  publishedDatasources(filter:{luid:$l}){
    name
    downstreamWorkbooks{ luid name projectName }
  }
}
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pds-luid", required=True)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    with signed_in_server() as server:
        d = graphql(server, DOWNSTREAM, {"l": args.pds_luid}).get(
            "publishedDatasources") or []
        if not d:
            raise SystemExit(f"PDS not found: {args.pds_luid}")
        pds = d[0]
        downstream = pds.get("downstreamWorkbooks") or []

        workbooks = []
        for wb in downstream:
            rec = {"luid": wb["luid"], "name": wb["name"],
                   "project": wb.get("projectName"), "views": {}, "error": ""}
            # luid prefix keeps same-named workbooks in distinct directories
            wb_dir = out / f"{_slug(wb['name'])}_{wb['luid'][:8]}"
            try:
                item = server.workbooks.get_by_id(wb["luid"])
                wb_dir.mkdir(exist_ok=True)
                rec["views"] = export_view_images(server, item, "view", wb_dir)
            except Exception as e:  # one broken workbook must not stop the sweep
                rec["error"] = str(e)[:200]
            workbooks.append(rec)

    view_errors = [{"workbook": w["name"], "view": vname, "error": v["error"]}
                   for w in workbooks
                   for vname, v in w["views"].items() if v["error"]]
    workbook_errors = [{"workbook": w["name"], "error": w["error"]}
                       for w in workbooks if w["error"]]
    total_views = sum(len(w["views"]) for w in workbooks)
    tally = {
        "workbooks": len(workbooks),
        "views": total_views,
        "ok": total_views - len(view_errors),
        "error": len(view_errors),
        "workbook_errors": len(workbook_errors),
    }

    result = {
        "pds_luid": args.pds_luid,
        "pds_name": pds["name"],
        "tally": tally,
        # errors carry the raw message (first 200 chars, truncated at capture)
        "errors": view_errors,
        "workbook_errors": workbook_errors,
        "workbooks": workbooks,
        "verified": not view_errors and not workbook_errors,
    }
    (out / "result.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    # stdout carries the summary only: the sweep scales with the whole
    # downstream set, and the full per-view record is already in result.json
    summary = {k: result[k] for k in
               ("pds_name", "tally", "errors", "workbook_errors", "verified")}
    print("RESULT_JSON:", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
