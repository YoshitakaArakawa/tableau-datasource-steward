"""メタデータ整備バッチの orchestrator。コスト見積もり（estimate）と一括実行（run）。

estimate: ユーザーが指定した範囲（project / PDS 名 / LUID）に対し、per-PDS で
  「source 列経路（in-place）で埋められる数 / republish が必要な数 / extract サイズ」
  を算出する。republish 必至の件数と extract 総量が大きいときは、着手前に
  「全部やると大変」という警告を出す（対象の絞り直しを促すゲート）。

run: spec ディレクトリ（1 PDS = 1 spec.json）を進捗 manifest 付きで一括実行する。
  - 開始前に認証前チェック（cached session が無ければ即エラー。ブラウザ待ちハング防止）
  - spec ごとに実行前ゲート: spec の field_caption が実スキーマに存在するか照合
    （余剰キー = 中止。読み違いの spec を publish させない）
  - augment_datasource.py を subprocess 実行し、RESULT_JSON を manifest に記録
  - 中断後は --resume で再開（done の spec はスキップ）

usage:
    python batch_augment.py estimate --projects stg,intermediate,marts --out estimate.json
    python batch_augment.py run --spec-dir specs/ --out-root out/ [--resume]
"""
from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import requests  # noqa: E402
from tableau_auth import signed_in_server  # noqa: E402
from metadata_api import graphql  # noqa: E402

AUGMENT = REPO / ".claude/skills/datasource-augmenter/scripts/augment_datasource.py"
READ_SCHEMA = REPO / ".claude/skills/datasource-inspector/scripts/read_schema.py"

# 警告しきい値。republish は 1 件ごとに extract 込み .tdsx の DL/UL 往復が要るため、
# 件数と総量の両方で「大変さ」を見積もる（値は 45 PDS 一括実験の体感に基づく目安）
WARN_REPUBLISH_COUNT = 10
WARN_TOTAL_SIZE_MB = 200


# --- estimate ------------------------------------------------------------------
ESTIMATE_QUERY = """
query {
  publishedDatasources {
    name luid projectName description
    upstreamTables { name }
    fields {
      __typename name description
      ... on ColumnField {
        dataType
        upstreamColumns { table { __typename name ... on DatabaseTable { luid } } }
      }
    }
  }
}
"""


def cmd_estimate(server, projects: set[str], names: set[str], luids: set[str],
                 out_path: Path) -> None:
    rest = requests.get(
        f"{server.server_address}/api/{server.version}/sites/{server.site_id}/datasources",
        headers={"X-Tableau-Auth": server.auth_token, "Accept": "application/json"},
        params={"pageSize": 1000}, timeout=60).json()
    rest_by_luid = {d["id"]: d for d in rest["datasources"]["datasource"]}
    rest_tables = requests.get(
        f"{server.server_address}/api/{server.version}/sites/{server.site_id}/tables",
        headers={"X-Tableau-Auth": server.auth_token, "Accept": "application/json"},
        params={"pageSize": 1000}, timeout=60).json()
    addressable = {t["id"] for t in (rest_tables.get("tables", {}) or {}).get("table", [])}

    rows = []
    for ds in graphql(server, ESTIMATE_QUERY)["publishedDatasources"]:
        if projects and ds["projectName"] not in projects:
            continue
        if names and ds["name"] not in names:
            continue
        if luids and ds["luid"] not in luids:
            continue
        table_names = {t["name"] for t in ds.get("upstreamTables") or []}
        undescribed_cols, calc_undescribed, source_eligible = [], [], []
        for f in ds["fields"]:
            ups = f.get("upstreamColumns")
            # 論理テーブル擬似列（dataType=TABLE が確定的目印。Custom SQL は
            # upstreamTables が空になるため名前一致だけでは拾えない）
            if (f["__typename"] == "ColumnField" and not ups
                    and (f.get("dataType") == "TABLE" or f["name"] in table_names)):
                continue
            if (f.get("description") or "").strip():
                continue
            if f["__typename"] == "CalculatedField":
                calc_undescribed.append(f["name"])
                continue
            undescribed_cols.append(f["name"])
            if (len(ups or []) == 1
                    and ((ups[0].get("table") or {}).get("luid") or "") in addressable):
                source_eligible.append(f["name"])
        r = rest_by_luid.get(ds["luid"], {})
        n_republish = len(undescribed_cols) - len(source_eligible) + len(calc_undescribed)
        rows.append({
            "name": ds["name"], "luid": ds["luid"], "project": ds["projectName"],
            "grain_missing": not (ds.get("description") or "").strip(),
            "has_extracts": str(r.get("hasExtracts", "")).lower() == "true",
            "size_mb": int(r.get("size", 0)),
            "n_undescribed_columns": len(undescribed_cols),
            "n_undescribed_calcs": len(calc_undescribed),
            "n_source_column_eligible": len(source_eligible),
            "n_republish_needed_fields": n_republish,
            # フィールド desc がソース列経路で埋まらず republish が要るか。
            # grain だけの整備も republish は要るが、負荷見積もり上は分けて数える
            "republish_required": n_republish > 0,
        })

    republish = [r for r in rows if r["republish_required"]]
    total_mb = sum(r["size_mb"] for r in republish if r["has_extracts"])
    summary = {
        "n_datasources": len(rows),
        "n_source_column_only": len(rows) - len(republish),
        "n_republish_required": len(republish),
        "n_grain_missing": sum(1 for r in rows if r["grain_missing"]),
        "republish_total_extract_mb": total_mb,
        "heavy": len(republish) >= WARN_REPUBLISH_COUNT or total_mb >= WARN_TOTAL_SIZE_MB,
    }
    out = {"summary": summary, "datasources": sorted(
        rows, key=lambda r: (-r["n_republish_needed_fields"], r["name"]))}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("RESULT_JSON:", json.dumps(summary, ensure_ascii=False))
    if summary["heavy"]:
        print(f"WARNING: republish 必至 {len(republish)} 件・extract 合計 {total_mb} MB。"
              "全件往復は時間がかかる。対象の絞り直しを検討（詳細は estimate 出力）")


# --- run -----------------------------------------------------------------------
def _auth_precheck() -> None:
    r = subprocess.run([sys.executable, str(REPO / "scripts/tableau_auth.py"), "status"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            "auth precheck failed: cached session が無いか失効。先に signed_in_server() で"
            f"サインインしてから再実行する。\n{r.stdout.strip()}")


def _schema_names(out_dir: Path, pds_luid: str) -> set[str] | None:
    schema_path = out_dir / "schema.json"
    r = subprocess.run([sys.executable, str(READ_SCHEMA),
                        "--pds-luid", pds_luid, "--out", str(schema_path)],
                       capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        return None
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return ({c["name"] for c in schema["columns"]}
            | {c["name"] for c in schema["calcs"]})


def cmd_run(spec_dir: Path, out_root: Path, resume: bool) -> None:
    _auth_precheck()
    specs = sorted(spec_dir.glob("*.json"))
    if not specs:
        raise SystemExit(f"no spec files in {spec_dir}")
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / "manifest.json"
    manifest = (json.loads(manifest_path.read_text(encoding="utf-8"))
                if (resume and manifest_path.exists()) else {"specs": {}})

    def save():
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                 encoding="utf-8")

    for spec_path in specs:
        key = spec_path.name
        entry = manifest["specs"].get(key, {})
        if resume and entry.get("status") == "done":
            continue
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        out_dir = out_root / spec_path.stem
        out_dir.mkdir(parents=True, exist_ok=True)
        entry = {"status": "running",
                 "started": datetime.datetime.now().isoformat(timespec="seconds")}
        manifest["specs"][key] = entry
        save()

        # 実行前ゲート: spec が指す field が実スキーマに存在するか（余剰キー検出）
        names = _schema_names(out_dir, spec["source_luid"])
        if names is not None:
            unknown = [d["field_caption"] for d in spec.get("descriptions", [])
                       if d["field_caption"] not in names]
            if unknown:
                entry.update(status="gate_failed",
                             error=f"spec の field が PDS に存在しない: {unknown}")
                save()
                continue
        else:
            entry["warning"] = "schema 取得に失敗（ゲートはスキップ、publish 側検証に委譲）"

        r = subprocess.run([sys.executable, str(AUGMENT),
                            "--spec", str(spec_path), "--out-dir", str(out_dir)],
                           capture_output=True, text=True, encoding="utf-8")
        result_line = next((l for l in (r.stdout or "").splitlines()
                            if l.startswith("RESULT_JSON:")), None)
        result = json.loads(result_line.split(":", 1)[1]) if result_line else None
        entry.update(
            status=("done" if r.returncode == 0 and result and result.get("verified")
                    else "failed"),
            finished=datetime.datetime.now().isoformat(timespec="seconds"),
            exit_code=r.returncode,
            published_luid=(result or {}).get("published_luid"),
            verified=(result or {}).get("verified"),
            aborted=(result or {}).get("aborted"),
        )
        if entry["status"] == "failed":
            entry["error"] = ((result or {}).get("aborted")
                              or (r.stderr or r.stdout or "")[-400:])
        save()

    counts: dict[str, int] = {}
    for e in manifest["specs"].values():
        counts[e["status"]] = counts.get(e["status"], 0) + 1
    print("RESULT_JSON:", json.dumps(
        {"manifest": str(manifest_path), "counts": counts}, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_est = sub.add_parser("estimate", help="対象範囲のコスト見積もり（読取のみ）")
    p_est.add_argument("--projects", default="", help="project 名のカンマ区切り")
    p_est.add_argument("--names", default="", help="PDS 名のカンマ区切り")
    p_est.add_argument("--luids", default="", help="PDS LUID のカンマ区切り")
    p_est.add_argument("--out", required=True)
    p_run = sub.add_parser("run", help="spec-dir を manifest 付きで一括実行")
    p_run.add_argument("--spec-dir", required=True)
    p_run.add_argument("--out-root", required=True)
    p_run.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    if args.cmd == "estimate":
        sel = {k: {s.strip() for s in getattr(args, k).split(",") if s.strip()}
               for k in ("projects", "names", "luids")}
        if not any(sel.values()):
            raise SystemExit("--projects / --names / --luids のいずれかで対象を指定する")
        with signed_in_server() as server:
            cmd_estimate(server, sel["projects"], sel["names"], sel["luids"],
                         Path(args.out))
    elif args.cmd == "run":
        cmd_run(Path(args.spec_dir), Path(args.out_root), args.resume)


if __name__ == "__main__":
    main()
