"""CreateNew で publish した draft PDS をガード付きで削除する。

削除対象は result.json の published_luid（authoritative な記録）または明示 LUID。
誤爆防止のため、**name が --suffix で終わる ∧ project が --projects に含まれる**
の両方を満たす PDS だけを削除する。既定は dry-run（一覧表示のみ）で、
実削除には --execute が必要。

usage:
    # dry-run（削除対象の確認）
    python cleanup_drafts.py --result-glob "out/*/result.json" --projects stg,marts
    # 実削除
    python cleanup_drafts.py --result-glob "out/*/result.json" --projects stg,marts --execute
    # LUID 直接指定
    python cleanup_drafts.py --luids LUID1 LUID2 --projects hoist_test --execute

Auth: OAuth (scripts/tableau_auth.py, signed_in_server())
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

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
import tableauserverclient as TSC  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--result-glob", action="append", default=[],
                    help="result.json の glob（published_luid を収集）。複数指定可")
    ap.add_argument("--luids", nargs="*", default=[], help="削除候補 LUID の直接指定")
    ap.add_argument("--suffix", default="__augmented",
                    help="削除を許す name の接尾辞ガード（既定 __augmented）")
    ap.add_argument("--projects", required=True,
                    help="削除を許す project 名のカンマ区切り（必須ガード）")
    ap.add_argument("--execute", action="store_true",
                    help="指定時のみ実削除（既定は dry-run）")
    args = ap.parse_args()

    luids: set[str] = set(args.luids)
    for pattern in args.result_glob:
        for f in glob.glob(pattern):
            d = json.loads(Path(f).read_text(encoding="utf-8"))
            if d.get("published_luid"):
                luids.add(d["published_luid"])
    if not luids:
        raise SystemExit("no candidates (--result-glob / --luids のどちらかを指定)")
    allowed_projects = {p.strip() for p in args.projects.split(",") if p.strip()}

    deleted, skipped, missing = [], [], []
    with signed_in_server() as server:
        for luid in sorted(luids):
            try:
                ds = server.datasources.get_by_id(luid)
            except TSC.ServerResponseError:
                missing.append(luid)
                continue
            entry = {"luid": luid, "name": ds.name, "project": ds.project_name}
            if not ds.name.endswith(args.suffix):
                skipped.append({**entry, "reason": f"name が {args.suffix} で終わらない"})
                continue
            if ds.project_name not in allowed_projects:
                skipped.append({**entry, "reason": f"project が対象外: {ds.project_name}"})
                continue
            if args.execute:
                server.datasources.delete(luid)
            deleted.append(entry)

    result = {"dry_run": not args.execute,
              "deleted" if args.execute else "would_delete": deleted,
              "skipped_by_guard": skipped, "missing": missing}
    print("RESULT_JSON:", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
