"""Exports: corpus manifest, edges, groups - and a self-checking bundle.

Every edge in every export carries ``similarity`` (exact Jaccard over phrase
sets), the phrase counts, the candidate-filter provenance and the review
decision. Groups are exported flat (``groups`` + ``group_members`` +
``group_edges``) rather than as cliques, and the manifest includes a
``pair_matrix`` for every group so an importer can see which member pairs have
no direct edge.

The archive is a zip of JSON/CSV files plus ``manifest.json``. No binary
formats, no external tooling required to read it.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from . import grouping, textrules
from .db import Row
from .store import Store, now


def _task_or_latest(store: Store, task_id: str | None) -> Row:
    task = store.get_task(task_id) if task_id else store.latest_task()
    if task is None:
        raise KeyError("no index task found")
    return task


def corpus_manifest(store: Store, task_id: str | None = None) -> dict[str, Any]:
    task = _task_or_latest(store, task_id)
    ruleset = store.get_ruleset(task["rules_fp"])
    docs = store.db.execute(
        "SELECT d.*, r.token_count AS rev_tokens, r.phrase_count AS rev_phrases"
        " FROM documents d LEFT JOIN doc_revisions r"
        "   ON r.doc_id=d.id AND r.rules_fp=?"
        " ORDER BY d.created_at, d.id", (task["rules_fp"],)).fetchall()
    groups = grouping.group_detail_rows(store, task["id"])
    edges = grouping.all_edges(store, task["id"])
    return {
        "export_version": "corpuslake.manifest.v1",
        "exported_at": now(),
        "task": {
            "id": task["id"], "mode": task["task_mode"], "status": task["status"],
            "threshold": task["threshold"], "rules_fp": task["rules_fp"],
            "vocab_digest": task["vocab_digest"], "note": task["note"],
            "started_at": task["started_at"], "finished_at": task["finished_at"],
            "snapshot": json.loads(task["snapshot_json"]) if task["snapshot_json"] else None,
            "completeness_claim": (
                "exact mode: every document pair in the snapshot was compared; "
                "the edge list is complete for this snapshot and rule set."
                if task["task_mode"] == "exact" else
                "approx mode: candidates came from MinHash/LSH and may be "
                "incomplete; stored scores are exact but the list is not claimed "
                "to be complete."),
        },
        "rules": textrules.describe_rules(
            ruleset["tokenizer_version"], ruleset["phrase_version"],
            int(ruleset["phrase_n"]), ruleset["vocab_version"]),
        "documents": [{
            "id": d["id"], "title": d["title"], "source": d["source"],
            "source_ref": d["source_ref"], "content_hash": d["content_hash"],
            "char_count": d["char_count"], "token_count": d["rev_tokens"],
            "phrase_count": d["rev_phrases"], "is_empty": bool(d["is_empty"]),
            "duplicate_of": d["duplicate_of"], "status": d["status"],
            "created_at": d["created_at"], "withdrawn_at": d["withdrawn_at"],
            "withdrawn_reason": d["withdrawn_reason"],
            "text": d["text"],
        } for d in docs],
        "edges": [grouping.edge_public(e) for e in edges],
        "groups": groups,
        "similarity_semantics": {
            "metric": "jaccard over deduplicated phrase sets",
            "not_transitive": True,
            "group_kind": "connected component of accepted edges",
            "warning": ("Grouping is a review aid over edges, not a clique. "
                        "Two members of the same group may have no direct edge "
                        "and may score below the threshold."),
            "approx_mode": task["task_mode"] == "approx",
        },
    }


def edges_rows(store: Store, task_id: str | None = None) -> list[dict[str, Any]]:
    task = _task_or_latest(store, task_id)
    rows = grouping.all_edges(store, task["id"])
    return [grouping.edge_public(r) for r in rows]


def edges_csv(store: Store, task_id: str | None = None) -> str:
    rows = edges_rows(store, task_id)
    buf = io.StringIO()
    fields = ["edge_id", "task_id", "doc_a", "doc_b", "jaccard", "intersection",
              "union_size", "provenance", "candidate_reason", "estimated_jaccard",
              "decision"]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def links_nquads(store: Store, task_id: str | None = None) -> str:
    """Edge list as N-Quads-ish ``<docA> <sim> <docB> <score> .`` lines."""
    task = _task_or_latest(store, task_id)
    lines = []
    for row in grouping.all_edges(store, task["id"]):
        lines.append('<%s> <urn:corpuslake:jaccard> <%s> "%.6f"^^<http://www.w3.org/2001/XMLSchema#double> .'
                     % (row["doc_a"], row["doc_b"], row["jaccard"]))
    return "\n".join(lines) + ("\n" if lines else "")


def export_bundle(store: Store, dest_dir: Path, task_id: str | None = None) -> Path:
    """Write a zip bundle and return its path."""
    task = _task_or_latest(store, task_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = now()
    path = dest_dir / ("corpuslake_export_%s_%d.zip" % (task["id"], int(stamp)))
    manifest = corpus_manifest(store, task["id"])
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("edges.json", json.dumps(manifest["edges"], ensure_ascii=False, indent=2))
        zf.writestr("edges.csv", edges_csv(store, task["id"]))
        zf.writestr("groups.json", json.dumps(manifest["groups"], ensure_ascii=False, indent=2))
        zf.writestr("groups_flat.json",
                    json.dumps(groups_document(store, task["id"]),
                               ensure_ascii=False, indent=2))
        zf.writestr("links.nq", links_nquads(store, task["id"]))
        zf.writestr("documents.csv", documents_csv(store, task["id"]))
        zf.writestr("rules.json", json.dumps(manifest["rules"], ensure_ascii=False, indent=2))
    return path


def groups_document(store: Store, task_id: str | None = None) -> dict[str, Any]:
    """Groups plus their explicit member-pair matrix (no implied cliques)."""
    task = _task_or_latest(store, task_id)
    groups = grouping.group_detail_rows(store, task["id"])
    return {
        "export_version": "corpuslake.groups.v1",
        "task_id": task["id"],
        "group_kind": "connected component of accepted edges",
        "not_transitive": True,
        "warning": ("A group is not a clique. ``pairs`` lists every member pair; "
                    "pairs whose ``edge`` is null have no direct edge and did not "
                    "reach the threshold."),
        "groups": [{
            "group_id": g["group"]["id"],
            "label": g["group"]["label"],
            "algorithm": g["group"]["algorithm"],
            "members": [{"id": m["id"], "title": m["title"], "status": m["status"]}
                        for m in g["members"]],
            "edges": g["edges"],
            "pairs": [{
                "doc_a": p["doc_a"], "doc_b": p["doc_b"],
                "edge_id": p["edge"]["edge_id"] if p["edge"] else None,
                "jaccard": p["edge"]["jaccard"] if p["edge"] else None,
                "decision": p["edge"]["decision"] if p["edge"] else None,
            } for p in g["pairs"]],
        } for g in groups],
    }


def documents_csv(store: Store, task_id: str | None = None) -> str:
    task = _task_or_latest(store, task_id)
    docs = store.db.execute(
        "SELECT id,title,source,source_ref,content_hash,char_count,token_count,"
        "phrase_count,is_empty,duplicate_of,status,created_at,withdrawn_at"
        " FROM documents ORDER BY created_at, id").fetchall()
    buf = io.StringIO()
    fields = ["id", "title", "source", "source_ref", "content_hash", "char_count",
              "token_count", "phrase_count", "is_empty", "duplicate_of", "status",
              "created_at", "withdrawn_at"]
    writer = csv.DictWriter(buf, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for d in docs:
        writer.writerow({f: d[f] for f in fields})
    return buf.getvalue()
