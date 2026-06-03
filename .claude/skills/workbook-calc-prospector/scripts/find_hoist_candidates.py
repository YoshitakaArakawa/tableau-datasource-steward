"""Find calculated fields duplicated across the downstream workbooks of a PDS,
and propose hoist candidates (calcs to consolidate into the published data source).

Pipeline: target PDS -> downstream workbooks (Metadata API) -> their embedded
CalculatedFields -> normalize formula (comment-stripped key) -> group across
workbooks -> classify hoistability (operands present in the PDS, not a table
calc). Emits a change-set fragment compatible with datasource-augmenter (calcs[]).

Per group we also carry the structured WB-side descriptions (the calc's own
`description` field) and the raw formula WITH comments, as extraction material
for datasource-column-describer. We do NOT decide here whether a comment is a
description -- that semantic call is the describer's (ANALYZE layer) job.

usage:
    python find_hoist_candidates.py --pds-luid <luid> --out candidates.json
                                    [--min-workbooks 2]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
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

# table calc 関数は view 文脈依存で PDS 側 calc に寄せると意味が変わるため hoist 不可扱い
TABLE_CALC_FUNCS = (
    "WINDOW_", "INDEX", "LOOKUP", "RUNNING_", "FIRST", "LAST",
    "RANK", "TOTAL", "SIZE", "PREVIOUS_VALUE",
)

DOWNSTREAM = """
query($l:String!){
  publishedDatasources(filter:{luid:$l}){
    name fields{ name }
    downstreamWorkbooks{ name luid }
  }
}
"""

WB_CALCS = """
query($l:String!){
  workbooks(filter:{luid:$l}){
    embeddedDatasources{ fields{ name description __typename ... on CalculatedField{ formula } } }
  }
}
"""


def strip_comments(formula: str) -> str:
    # Tableau calc comments: /* block */ and // line. Strip both so that two
    # formulas identical except for comments collapse into one dedup group.
    # 既知の限界: 文字列リテラル内の // も剥がすが（例 "http://"）、dedup キーは
    # 重複間で一貫していれば足り、PDS に注入する raw formula 側は別途保持する。
    no_block = re.sub(r"/\*.*?\*/", " ", formula, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", no_block)


def normalize(formula: str) -> str:
    # dedup キーはコメント除去後の formula から作る（コメント差で別 calc 扱いを防ぐ）
    return re.sub(r"\s+", " ", strip_comments(formula).strip())


def operands(formula: str) -> list[str]:
    # [field] 参照を抽出（関数名は () が続くので除外しきれないが近似で十分）
    return sorted(set(re.findall(r"\[([^\[\]]+)\]", formula)))


def is_table_calc(formula: str) -> bool:
    up = formula.upper()
    return any(fn in up for fn in TABLE_CALC_FUNCS)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pds-luid", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--min-workbooks", type=int, default=2,
                    help="同一 formula がこの数以上の workbook に出れば候補")
    args = ap.parse_args()

    with signed_in_server() as server:
        d = graphql(server, DOWNSTREAM, {"l": args.pds_luid})["publishedDatasources"]
        if not d:
            raise SystemExit(f"PDS not found: {args.pds_luid}")
        pds = d[0]
        pds_fields = {f["name"] for f in pds["fields"]}
        workbooks = pds.get("downstreamWorkbooks") or []

        # comment-stripped formula -> set(workbook名), 代表 caption, raw formula(コメント込み), WB-side desc
        groups = defaultdict(lambda: {"workbooks": set(), "captions": set(),
                                      "raw": None, "wb_descriptions": set()})
        for wb in workbooks:
            wbres = graphql(server, WB_CALCS, {"l": wb["luid"]}).get("workbooks") or []
            for w in wbres:
                for eds in w.get("embeddedDatasources") or []:
                    for f in eds.get("fields") or []:
                        if f.get("__typename") == "CalculatedField" and f.get("formula"):
                            key = normalize(f["formula"])
                            g = groups[key]
                            g["workbooks"].add(wb["name"])
                            g["captions"].add(f["name"])
                            g["raw"] = f["formula"]  # コメント込みの生 formula（describer 用）
                            desc = (f.get("description") or "").strip()
                            if desc:
                                g["wb_descriptions"].add(desc)

    candidates = []
    for norm, g in groups.items():
        if len(g["workbooks"]) < args.min_workbooks:
            continue
        ops = operands(norm)
        missing = [o for o in ops if o not in pds_fields]
        tablecalc = is_table_calc(norm)
        reasons = []
        if tablecalc:
            reasons.append("table-calc（view 文脈依存。PDS calc 化で意味が変わりうる）")
        if missing:
            reasons.append(f"operand が PDS に不在: {missing}")
        candidates.append({
            "suggested_caption": sorted(g["captions"])[0],
            "formula": g["raw"],  # コメント込み生 formula。describer がコメントを手がかりに使う
            "workbooks": sorted(g["workbooks"]),
            "workbook_count": len(g["workbooks"]),
            "operands": ops,
            "wb_descriptions": sorted(g["wb_descriptions"]),  # 構造化 desc の機械抽出（推論なし）
            "hoistable": not tablecalc and not missing,
            "reasons": reasons,
        })

    candidates.sort(key=lambda c: (-c["workbook_count"], c["suggested_caption"]))
    out = {
        "pds_luid": args.pds_luid,
        "pds_name": pds["name"],
        "downstream_workbooks": len(workbooks),
        "candidate_count": len(candidates),
        "hoistable_count": sum(c["hoistable"] for c in candidates),
        "candidates": candidates,
    }
    Path(args.out).write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print("RESULT_JSON:", json.dumps({k: out[k] for k in
          ("pds_name", "downstream_workbooks", "candidate_count", "hoistable_count")},
          ensure_ascii=False))


if __name__ == "__main__":
    main()
