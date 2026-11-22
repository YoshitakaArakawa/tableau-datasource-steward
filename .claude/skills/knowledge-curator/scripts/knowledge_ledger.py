"""Business Preference 台帳の参照抽出・参照切れ点検・Knowledge との同期計画。

Knowledge への書き込みは行わない（書き込みは Tableau MCP の manage-knowledge-context）。
本スクリプトは決定的に判定できる部分だけを担う。

subcommands:
    init   Knowledge のスナップショットから台帳を作る（台帳が無いとき）
    refs   台帳の statements からバッククォートの名前を抜き、depends_on を更新する（オフライン）
    check  参照名を実在する PDS 名・フィールド名と照合する（参照切れ・曖昧・未引用）
    plan   台帳と Knowledge のスナップショットを3方向比較し、create / update / pull を計画する

usage:
    python knowledge_ledger.py init  --snapshot knowledge.json [--snapshot more.json] --ledger bp.json
    python knowledge_ledger.py refs  --ledger bp.json [--write-back]
    python knowledge_ledger.py check --ledger bp.json --out check.json
                                     [--catalog catalog.json | --save-catalog catalog.json]
                                     [--text-file skill.md ...]
    python knowledge_ledger.py plan  --ledger bp.json --snapshot knowledge.json [--snapshot more.json]
                                     --check check.json --out plan.json [--write-back]
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

# Windows コンソール (cp932) の文字化け対策
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# セットアップ時に置換されるプレースホルダ（{{NAME}}）。名前の照合対象から外す
PLACEHOLDER = re.compile(r"\{\{[^}]*\}\}")
BACKTICK = re.compile(r"`([^`\n]+)`")
# `field` in `datasource` の対（in の前後は空白のみ許す）
FIELD_IN_DS = re.compile(r"`([^`\n]+)`\s+in\s+`([^`\n]+)`")
# 名前の中の <table> のような可変部。glob の * に書き換えるべきもの
ANGLE_PLACEHOLDER = re.compile(r"<[^<>]+>")
# Markdown の fenced code block。コマンド例であって名前の参照ではない
FENCED = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)
# 二重バッククォートの code span（`` `X` in `Y` `` のような書式の例示）。参照ではない
DOUBLE_TICK_SPAN = re.compile(r"``.+?``")
# 二重引用符で囲んだ語は「問いの言葉」（C2 の解釈対象）で、名前の参照ではない
DOUBLE_QUOTED = re.compile(r'"[^"\n]*"|“[^”\n]*”')
# 未引用検出で拾う名前の最短長。2文字以下は地の文と衝突しやすい
MIN_UNQUOTED_LEN = 3
# Metadata API の 1 ページ件数（connection の first 上限に収まる値）
PAGE_SIZE = 100
# 名前の境界。ASCII の語の途中（Revenues の Revenue）だけを除き、日本語の地の文に
# 埋まった名前（売上はRevenueを使う）は拾う
WORD_EDGE_L = r"(?<![A-Za-z0-9_])"
WORD_EDGE_R = r"(?![A-Za-z0-9_])"
# name の kind 接頭辞（"Term: ..."）
KIND_PREFIX = re.compile(r"^(Usage|Term|Method|Cross-source metric):\s")


def is_glob(name: str) -> bool:
    return "*" in name or "?" in name


# --- 台帳 ------------------------------------------------------------------------
# 台帳の item は利用者が書いた形のまま保存する。読み替えは下のアクセサで行い、
# 読み替えた値を item に書き戻さない（title/text 形式が二重保存されないように）。
def item_name(it: dict) -> str:
    return it.get("name") or it.get("title") or ""


def item_statements(it: dict) -> list[str]:
    if isinstance(it.get("statements"), list):
        return [str(s) for s in it["statements"]]
    single = it.get("statement") or it.get("text")
    return [single] if single else []


def set_statements(it: dict, statements: list[str]) -> None:
    """元の形を保って書き戻す。1 本なら元のキー、複数なら statements に移す。"""
    if "statements" in it or len(statements) != 1:
        it.pop("statement", None)
        it.pop("text", None)
        it["statements"] = statements
    elif "text" in it and "statement" not in it:
        it["text"] = statements[0]
    else:
        it["statement"] = statements[0]


def item_scope(it: dict) -> list:
    if it.get("scope") == "node":
        return ["node", it.get("target_node_id")]
    return ["global", None]


def set_scope(it: dict, scope: list) -> None:
    it["scope"] = scope[0]
    it["target_node_id"] = scope[1]


def load_ledger(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    names = []
    for it in data.get("items", []):
        if not item_name(it) or not item_statements(it):
            raise SystemExit(f"ledger item lacks name/statements: {json.dumps(it, ensure_ascii=False)[:200]}")
        if it.get("scope") == "node" and not it.get("target_node_id"):
            raise SystemExit(f"scope=node needs target_node_id: {item_name(it)}")
        names.append(item_name(it))
    dup = sorted({n for n in names if names.count(n) > 1})
    if dup:
        raise SystemExit(f"duplicate item names in ledger (name is the sync key): {dup}")
    return data


def save_json(path: str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


# --- 参照の抽出 ------------------------------------------------------------------
def extract_refs(text: str) -> list[dict]:
    """バッククォートの名前を抜く。`X` in `Y` は field X in datasource Y。"""
    text = PLACEHOLDER.sub("", text)
    refs, consumed = [], set()
    for m in FIELD_IN_DS.finditer(text):
        refs.append({"kind": "field", "field": m.group(1), "datasource": m.group(2)})
        consumed.add(m.start(1))
        consumed.add(m.start(2))
    for m in BACKTICK.finditer(text):
        if m.start(1) in consumed:
            continue
        refs.append({"kind": "name", "name": m.group(1)})  # datasource か未修飾の field
    return refs


def ref_names(refs: list[dict]) -> list[str]:
    out = []
    for r in refs:
        out.append(f"{r['field']} in {r['datasource']}" if r["kind"] == "field" else r["name"])
    return sorted(set(out))


def cmd_refs(args) -> None:
    ledger = load_ledger(args.ledger)
    rows = []
    for it in ledger["items"]:
        names = ref_names(extract_refs("\n".join(item_statements(it))))
        rows.append({"name": item_name(it), "depends_on": names})
        if args.write_back:
            it["depends_on"] = names
    if args.write_back:
        save_json(args.ledger, ledger)
    print(json.dumps(rows, indent=2, ensure_ascii=False))


# --- カタログ（実在する名前） ---------------------------------------------------------
def _repo_modules():
    cur = Path(__file__).resolve()
    for _ in range(8):
        cur = cur.parent
        if (cur / "scripts" / "tableau_auth.py").exists():
            sys.path.insert(0, str(cur / "scripts"))
            break
    else:
        raise RuntimeError("repo-root scripts/ (with tableau_auth.py) not found")
    from tableau_auth import signed_in_server  # noqa: E402  (OAuth)
    from metadata_api import graphql  # noqa: E402
    return signed_in_server, graphql


LIST_Q = ("query($first:Int!,$after:String){publishedDatasourcesConnection("
          "first:$first,after:$after){nodes{name luid projectName}"
          "pageInfo{hasNextPage endCursor}}}")
FIELDS_Q = ("query($l:String!){publishedDatasources(filter:{luid:$l}){"
            "fields{name description __typename}}}")


def fetch_catalog(server, graphql) -> dict:
    """サイトの全 PDS 名を読む。フィールドは参照された PDS だけ後で読む（fields=None は未取得）。"""
    out, after = [], None
    while True:
        conn = graphql(server, LIST_Q, {"first": PAGE_SIZE, "after": after})[
            "publishedDatasourcesConnection"]
        out.extend({"name": n["name"], "luid": n["luid"], "project": n.get("projectName"),
                    "fields": None} for n in conn["nodes"])
        if not conn["pageInfo"]["hasNextPage"]:
            return {"datasources": out}
        after = conn["pageInfo"]["endCursor"]


def make_fetcher(server, graphql):
    def fetcher(luid: str) -> list[dict]:
        res = graphql(server, FIELDS_Q, {"l": luid})["publishedDatasources"]
        return [{"name": f["name"], "description": f.get("description"),
                 "type": f["__typename"]} for f in (res[0]["fields"] if res else [])]
    return fetcher


def ensure_fields(ds: dict, fetcher) -> list[dict] | None:
    """必要になった PDS だけフィールドを読む。オフラインで未取得なら None（判定不能）。"""
    if ds.get("fields") is None and fetcher:
        ds["fields"] = fetcher(ds["luid"])
    return ds.get("fields")


# --- 照合 -----------------------------------------------------------------------
def match_datasources(catalog: dict, name: str) -> list[dict]:
    if is_glob(name):
        return [d for d in catalog["datasources"] if fnmatch.fnmatchcase(d["name"], name)]
    return [d for d in catalog["datasources"] if d["name"] == name]


def field_names(fields: list[dict] | None) -> set[str]:
    return {f["name"] for f in fields or []}


def siblings(name: str, names: set[str]) -> list[str]:
    """`Shipped Date` に対する `Shipped Date (shipment_lines.csv)` のような同名列。"""
    return sorted(n for n in names if n != name and n.startswith(name + " ("))


def strip_quoted(text: str) -> str:
    return DOUBLE_QUOTED.sub(" ", BACKTICK.sub(" ", PLACEHOLDER.sub(" ", text)))


def find_unquoted(text: str, names: set[str]) -> list[str]:
    """引用されていない地の文に、実在する名前がそのまま出ていないか。"""
    plain = strip_quoted(text)
    hits = []
    for n in sorted(names, key=len, reverse=True):
        if len(n) < MIN_UNQUOTED_LEN:
            continue
        if re.search(WORD_EDGE_L + re.escape(n) + WORD_EDGE_R, plain):
            hits.append(n)
            plain = plain.replace(n, " ")  # 長い名前に含まれる短い名前を二重計上しない
    return hits


def check_text(text: str, catalog: dict, fetcher, text_mode: bool) -> dict:
    """1 件分の照合。text_mode はスキル等の Markdown 用で、名前らしくない引用を無視する。

    text_mode では fenced code と二重バッククォートの例示を除き、単独のバッククォートは「実在する名前・glob」に
    一致したときだけ参照とみなす（`python ...` や `--flag` を参照切れにしない）。
    """
    if text_mode:
        text = DOUBLE_TICK_SPAN.sub(" ", FENCED.sub(" ", text))
    refs = extract_refs(text)
    findings = []
    all_ds_names = {d["name"] for d in catalog["datasources"]}
    ds_in_scope: list[dict] = []
    pending_fields = []
    resolved: list[tuple[str, dict]] = []  # (field 名, PDS) の解決済みの対

    # 1) データソース参照を先に解決する（未修飾の field の候補範囲になる）
    for r in refs:
        ds_name = r["datasource"] if r["kind"] == "field" else r["name"]
        if ANGLE_PLACEHOLDER.search(ds_name):
            if text_mode and r["kind"] == "name":
                continue  # Markdown のパス表記（work/<date>/ 等）
            glob = ANGLE_PLACEHOLDER.sub("*", ds_name)
            findings.append({"status": "placeholder_in_name", "ref": ds_name,
                             "hint": f"可変部は * の glob で書く（`{glob}`）",
                             "glob_matches": len(match_datasources(catalog, glob))})
            continue
        matched = match_datasources(catalog, ds_name)
        if r["kind"] == "name" and not matched and not is_glob(ds_name):
            pending_fields.append(r)  # データソースでなければ未修飾 field の候補
            continue
        if not matched:
            if text_mode and r["kind"] == "name":
                continue
            findings.append({"status": "missing_datasource", "ref": ds_name,
                             **({"hint": "glob に一致するデータソースが無い"} if is_glob(ds_name) else {})})
            continue
        if not is_glob(ds_name) and len(matched) > 1:
            findings.append({"status": "duplicate_datasource_name", "ref": ds_name,
                             "projects": sorted(str(d.get("project")) for d in matched)})
        ds_in_scope.extend(matched)
        if r["kind"] != "field":
            findings.append({"status": "ok", "ref": ds_name,
                             "datasources": [d["name"] for d in matched]})
            continue
        ref = f"{r['field']} in {ds_name}"
        known = [d for d in matched if ensure_fields(d, fetcher) is not None]
        hits = [d for d in known if r["field"] in field_names(d["fields"])]
        if not hits:
            if len(known) < len(matched):
                findings.append({"status": "unknown_fields", "ref": ref,
                                 "hint": "カタログにフィールドが保存されていない。オンラインで再実行する"})
            else:
                findings.append({"status": "missing_field", "ref": ref})
            continue
        resolved.extend((r["field"], d) for d in hits)
        sib = sorted({s for d in hits for s in siblings(r["field"], field_names(d["fields"]))})
        if sib:
            findings.append({"status": "ambiguous_field", "ref": ref, "siblings": sib})
        else:
            findings.append({"status": "ok", "ref": ref, "datasources": [d["name"] for d in hits]})

    # 2) データソースでなかった単独の名前 = 未修飾の field
    for r in pending_fields:
        known = [d for d in ds_in_scope if ensure_fields(d, fetcher) is not None]
        hits = [d for d in known if r["name"] in field_names(d["fields"])]
        if hits:
            resolved.extend((r["name"], d) for d in hits)
            f = {"status": "unqualified_field", "ref": r["name"],
                 "datasources": sorted({d["name"] for d in hits}),
                 "hint": "`field` in `datasource` の形で修飾する"}
            sib = sorted({s for d in hits for s in siblings(r["name"], field_names(d["fields"]))})
            if sib:
                f["siblings"] = sib
                f["hint"] += "。同名の列があるので、どちらかを正確な名前で書く"
            findings.append(f)
        elif text_mode:
            continue  # 名前ではない引用（コマンド・パス・オプション等）
        elif not ds_in_scope:
            findings.append({"status": "unresolved", "ref": r["name"],
                             "hint": "データソースでもなく、同じ文にデータソースの参照も無い"})
        elif len(known) < len(ds_in_scope):
            findings.append({"status": "unknown_fields", "ref": r["name"],
                             "hint": "カタログにフィールドが保存されていない。オンラインで再実行する"})
        else:
            findings.append({"status": "missing_field", "ref": r["name"],
                             "searched": sorted({d["name"] for d in ds_in_scope})})

    # 3) 地の文に出ている実在名（引用漏れ）
    for n in find_unquoted(text, all_ds_names):
        findings.append({"status": "unquoted_datasource", "ref": n})
        ds_in_scope.extend(match_datasources(catalog, n))
    uniq = list({id(d): d for d in ds_in_scope}.values())
    scope_fields: set[str] = set()
    for d in uniq:
        scope_fields |= field_names(ensure_fields(d, fetcher))
    for n in find_unquoted(text, scope_fields):
        findings.append({"status": "unquoted_field", "ref": n})
        resolved.extend((n, d) for d in uniq if n in field_names(d.get("fields")))

    # 二重定義の判定材料: 参照先フィールドの現在の説明（判定は Skill 側で行う）
    defs, seen = [], set()
    for fname, d in resolved:
        key = (fname, d["luid"])
        if key in seen:
            continue
        seen.add(key)
        for fld in d.get("fields") or []:
            if fld["name"] == fname and fld.get("description"):
                defs.append({"field": fname, "datasource": d["name"],
                             "description": fld["description"]})
    return {"findings": findings, "field_descriptions": defs}


SEVERITY = {"missing_datasource": "error", "missing_field": "error", "unresolved": "error",
            "unknown_fields": "unknown",
            "ambiguous_field": "warning", "unqualified_field": "warning",
            "duplicate_datasource_name": "warning", "placeholder_in_name": "warning",
            "unquoted_datasource": "warning", "unquoted_field": "warning", "ok": "ok"}


def cmd_check(args) -> None:
    if args.catalog:
        catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
        run_check(args, catalog, None)
        return
    signed_in_server, graphql = _repo_modules()
    with signed_in_server() as server:
        run_check(args, fetch_catalog(server, graphql), make_fetcher(server, graphql))


def run_check(args, catalog: dict, fetcher) -> None:
    items = []
    if args.ledger:
        for it in load_ledger(args.ledger)["items"]:
            items.append({"source": "ledger", "name": item_name(it),
                          "text": "\n".join(item_statements(it)), "text_mode": False})
    for tf in args.text_file or []:
        items.append({"source": "text_file", "name": Path(tf).name,
                      "text": Path(tf).read_text(encoding="utf-8"), "text_mode": True})
    if not items:
        raise SystemExit("nothing to check: pass --ledger and/or --text-file")

    results, totals = [], {"error": 0, "unknown": 0, "warning": 0, "ok": 0}
    for it in items:
        r = check_text(it["text"], catalog, fetcher, it["text_mode"])
        for f in r["findings"]:
            f["severity"] = SEVERITY[f["status"]]
            totals[f["severity"]] += 1
        results.append({"source": it["source"], "name": it["name"], **r})

    save_json(args.out, {"totals": totals, "items": results})
    if args.save_catalog:
        # fields=null はこの回に読まなかった PDS。オフライン再実行では unknown_fields になる
        save_json(args.save_catalog, catalog)
    print("RESULT_JSON:", json.dumps({"items": len(results), **totals}, ensure_ascii=False))


# --- スナップショット -------------------------------------------------------------------
def _as_context_list(raw) -> list:
    if isinstance(raw, dict):
        if any(k in raw for k in ("contextId", "context_id", "id")):
            return [raw]  # 単一 context
        for key in ("contexts", "items", "data", "results"):
            if isinstance(raw.get(key), list):
                return raw[key]
    if isinstance(raw, list):
        return raw
    raise SystemExit("snapshot: expected a context, a list of contexts, or a dict wrapping one")


def load_snapshots(paths: list[str]) -> list[dict]:
    """inspect-knowledge-context list の出力（を保存した JSON、複数可）を正規形に寄せて結合する。

    正規形: {context_id, name, statements: [str], scope: [kind, node_id]}
    """
    out: dict[str, dict] = {}
    for path in paths:
        for c in _as_context_list(json.loads(Path(path).read_text(encoding="utf-8"))):
            cid = c.get("context_id") or c.get("contextId") or c.get("id")
            if not cid:
                raise SystemExit(f"snapshot entry lacks context id: {json.dumps(c, ensure_ascii=False)[:200]}")
            sts = c.get("statements")
            if isinstance(sts, list):
                statements = [s["statement"] if isinstance(s, dict) else str(s) for s in sts]
            else:
                single = c.get("statement") or c.get("text")
                statements = [single] if single else []
            node = c.get("node_id") or c.get("targetNodeId") or c.get("nodeId")
            is_global = c.get("is_global", c.get("isGlobal"))
            scope = ["global", None] if is_global or not node else ["node", node]
            out[cid] = {"context_id": cid, "name": c.get("name"), "statements": statements,
                        "scope": scope}
    return list(out.values())


def cmd_init(args) -> None:
    if Path(args.ledger).exists():
        raise SystemExit(f"ledger already exists: {args.ledger}（上書きしない）")
    items = []
    for c in load_snapshots(args.snapshot):
        m = KIND_PREFIX.match(c["name"] or "")
        it = {"kind": m.group(1) if m else None, "name": c["name"],
              "statements": c["statements"], "translation": None, "rationale": None}
        set_scope(it, c["scope"])
        it.update({"context_id": c["context_id"], "synced_statements": c["statements"],
                   "synced_name": c["name"], "synced_scope": c["scope"]})
        items.append(it)
    save_json(args.ledger, {"graph_id": args.graph_id, "items": items})
    print("RESULT_JSON:", json.dumps({"items": len(items)}, ensure_ascii=False))


# --- 同期計画 ---------------------------------------------------------------------
def _norm_list(xs) -> list[str]:
    return [re.sub(r"\s+", " ", str(x)).strip() for x in xs]


def three_way(mine, theirs, synced, norm=lambda v: v) -> str:
    """same / push（台帳側が変わった）/ pull（Knowledge 側が変わった）/ conflict。"""
    if norm(mine) == norm(theirs):
        return "same"
    if synced is not None and norm(synced) == norm(theirs):
        return "push"
    if synced is not None and norm(synced) == norm(mine):
        return "pull"
    return "conflict"


def _synced_statements(it: dict):
    if it.get("synced_statements") is not None:
        return it["synced_statements"]
    if it.get("synced_statement") is not None:  # 旧形式
        return [it["synced_statement"]]
    return None


def blocked_names(check_path: str | None) -> dict[str, list[str]]:
    """点検で error / unknown が出た台帳項目。これらは create / update しない（機械ゲート）。"""
    if not check_path:
        return {}
    data = json.loads(Path(check_path).read_text(encoding="utf-8"))
    out = {}
    for it in data.get("items", []):
        if it.get("source") != "ledger":
            continue
        bad = [f"{f['status']}: {f['ref']}" for f in it["findings"]
               if f["severity"] in ("error", "unknown")]
        if bad:
            out[it["name"]] = bad
    return out


def cmd_plan(args) -> None:
    ledger = load_ledger(args.ledger)
    snap = load_snapshots(args.snapshot)
    blocked = blocked_names(args.check)
    by_id = {c["context_id"]: c for c in snap}
    by_name: dict[str, list[dict]] = {}
    for c in snap:
        by_name.setdefault(c["name"], []).append(c)

    actions, linked = [], set()

    def push_write(action: dict, name: str) -> dict:
        if name in blocked:
            return {**action, "action": "blocked", "blocked_action": action["action"],
                    "reason": "点検で error / unknown がある。参照を直してから同期する",
                    "findings": blocked[name]}
        return action

    for it in ledger["items"]:
        name = item_name(it)
        k = by_id.get(it.get("context_id")) if it.get("context_id") else None
        if it.get("context_id") and not k:
            actions.append({"action": "recreate", "name": name, "context_id": it["context_id"],
                            "reason": "台帳の contextId が Knowledge に無い（外部で削除された可能性）。作り直すかは確認する"})
            continue
        if not k:
            cands = by_name.get(name, [])
            if len(cands) > 1:
                ids = [c["context_id"] for c in cands]
                linked.update(ids)
                actions.append({"action": "conflict", "name": name, "context_ids": ids,
                                "reason": "同名の context が Knowledge に複数ある"})
                continue
            if not cands:
                scope = item_scope(it)
                actions.append(push_write({"action": "create", "name": name,
                                           "statements": item_statements(it),
                                           "scope": scope[0], "target_node_id": scope[1]}, name))
                continue
            k = cands[0]
            it["context_id"] = k["context_id"]  # 名前で紐づけた（write-back 時に保存）
        linked.add(k["context_id"])

        dims = {
            "statements": three_way(item_statements(it), k["statements"], _synced_statements(it),
                                    _norm_list),
            "name": three_way(name, k["name"], it.get("synced_name")),
            "scope": three_way(item_scope(it), k["scope"], it.get("synced_scope")),
        }
        base = {"name": name, "context_id": k["context_id"]}
        conflicts = [d for d, v in dims.items() if v == "conflict"]
        if conflicts:
            actions.append({**base, "action": "conflict", "dimensions": conflicts,
                            "reason": "台帳と Knowledge の両方が前回同期から変わった（または未同期で値が違う）",
                            "ledger": {"name": name, "statements": item_statements(it),
                                       "scope": item_scope(it)},
                            "knowledge": {"name": k["name"], "statements": k["statements"],
                                          "scope": k["scope"]}})
            continue
        push = {d for d, v in dims.items() if v == "push"}
        pull = {d for d, v in dims.items() if v == "pull"}
        if push:
            upd = {**base, "action": "update", "fields": sorted(push),
                   "previous": {"name": k["name"], "statements": k["statements"], "scope": k["scope"]}}
            if "statements" in push:
                upd["statements"] = item_statements(it)
            if "name" in push:
                upd["new_name"] = name
            if "scope" in push:
                upd["scope"], upd["target_node_id"] = item_scope(it)
            actions.append(push_write(upd, name))
        if pull:
            actions.append({**base, "action": "pull", "fields": sorted(pull),
                            "knowledge": {"name": k["name"], "statements": k["statements"],
                                          "scope": k["scope"]}})
        if not push and not pull:
            actions.append({**base, "action": "unchanged"})
        if args.write_back:
            # Knowledge が正本。pull は台帳に取り込み、一致した次元は同期済みとして記録する
            if "statements" in pull:
                set_statements(it, k["statements"])
                it["translation_stale"] = True
            if "name" in pull:
                it["name"] = k["name"]
                it.pop("title", None)
            if "scope" in pull:
                set_scope(it, k["scope"])
            if "statements" not in push:
                it["synced_statements"] = k["statements"]
                it.pop("synced_statement", None)
            if "name" not in push:
                it["synced_name"] = k["name"]
            if "scope" not in push:
                it["synced_scope"] = k["scope"]

    for c in snap:
        if c["context_id"] not in linked:
            actions.append({"action": "orphan", "name": c["name"], "context_id": c["context_id"],
                            "statements": c["statements"],
                            "reason": "台帳に無い。台帳に取り込むか、削除（要承認）するかを確認する"})

    counts: dict[str, int] = {}
    for a in actions:
        counts[a["action"]] = counts.get(a["action"], 0) + 1
    save_json(args.out, {"graph_id": ledger.get("graph_id"), "counts": counts, "actions": actions})
    if args.write_back:
        save_json(args.ledger, ledger)
    print("RESULT_JSON:", json.dumps(counts, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="Knowledge のスナップショットから台帳を作る")
    p.add_argument("--snapshot", action="append", required=True, help="複数可（分けて読んだ list）")
    p.add_argument("--ledger", required=True, help="作る台帳のパス（既存なら中止）")
    p.add_argument("--graph-id")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("refs", help="statements の参照名を抜き depends_on を更新する")
    p.add_argument("--ledger", required=True)
    p.add_argument("--write-back", action="store_true")
    p.set_defaults(func=cmd_refs)

    p = sub.add_parser("check", help="参照名を実在する名前と照合する")
    p.add_argument("--ledger")
    p.add_argument("--text-file", action="append", help="スキル等の Markdown（複数可）")
    p.add_argument("--catalog", help="照合に使うカタログ JSON（省略時は Metadata API から読む）")
    p.add_argument("--save-catalog", help="読んだカタログを保存する（再実行・オフライン用）")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("plan", help="台帳と Knowledge のスナップショットから同期計画を作る")
    p.add_argument("--ledger", required=True)
    p.add_argument("--snapshot", action="append", required=True, help="複数可（分けて読んだ list）")
    p.add_argument("--check", help="check の結果。error / unknown のある項目の create / update を blocked にする")
    p.add_argument("--out", required=True)
    p.add_argument("--write-back", action="store_true",
                   help="contextId と同期済みの値（synced_*）、pull の取り込みを台帳に保存する")
    p.set_defaults(func=cmd_plan)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
