"""Independent verification of an index against full pairwise comparison.

On small samples we can afford the O(n^2) ground truth. ``verify_task``
recomputes every pair's Jaccard from the stored revisions - deliberately not
reusing the candidate filters - and compares it with the stored edges:

* ``missing``  - pairs at/above threshold with no stored edge (false negative);
* ``extra``    - stored edges below threshold (false positive);
* ``score_mismatch`` - stored score differs from the recomputed one.

Any missing edge means the candidate generation is not exact for that snapshot
and the task must not be reported as complete. For the default exact mode the
verification is expected to report zero missing edges.

A corpus above ``MAX_VERIFY_DOCS`` is verified on a deterministic sample of
documents by default: all pairs *within the sample* are still compared
exhaustively, but the report says ``scope = "sample"`` and never claims the
whole index is complete. ``full=True`` lifts the cap; ``sample_size`` controls
the sample. The sample is chosen by hashing the task id with each document id,
so the same task verifies the same sample every time.

``verify_pair`` recomputes one pair from scratch by re-tokenising the original
text (not the revisions), so it also checks the stored artefacts.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, field
from typing import Any

from . import indexing, textrules
from .db import Row
from .store import Store


MAX_VERIFY_DOCS = 400


def _sample_docs(task_id: str, doc_ids: list[str], size: int) -> list[str]:
    """Deterministic, rule-free sample: order by hash(task_id, doc_id)."""
    ranked = sorted(doc_ids,
                    key=lambda d: hashlib.sha256(("%s\x1f%s" % (task_id, d)).encode()).digest())
    return sorted(ranked[:size])


def _load_snapshot(store: Store, task: Row) -> dict[str, dict[str, Any]]:
    fp = task["rules_fp"]
    rows = store.db.execute(
        "SELECT d.id, d.content_hash, d.title, r.phrases_json, r.token_count"
        " FROM documents d LEFT JOIN doc_revisions r"
        "   ON r.doc_id=d.id AND r.rules_fp=?"
        " WHERE d.withdrawn_at IS NULL ORDER BY d.created_at, d.id", (fp,)).fetchall()
    return {
        r["id"]: {
            "title": r["title"], "content_hash": r["content_hash"],
            "phrases": [k for k, _c in json.loads(r["phrases_json"])]
            if r["phrases_json"] is not None else None,
            "token_count": r["token_count"],
        }
        for r in rows
    }


def verify_task(store: Store, task_id: str, sample_size: int = MAX_VERIFY_DOCS,
                full: bool = False) -> dict[str, Any]:
    task = store.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    full_snapshot = _load_snapshot(store, task)
    total_docs = len(full_snapshot)
    scope = "full"
    snapshot = full_snapshot
    if not full and total_docs > sample_size:
        chosen = set(_sample_docs(task_id, sorted(full_snapshot), sample_size))
        snapshot = {d: info for d, info in full_snapshot.items() if d in chosen}
        scope = "sample"
    elif full and total_docs > MAX_VERIFY_DOCS * 10:
        raise ValueError(
            "refusing a full verification of %d documents (%d pairs); verify a "
            "sample, or raise the cap in verification.py"
            % (total_docs, total_docs * (total_docs - 1) // 2))
    threshold = float(task["threshold"])
    ruleset = store.get_ruleset(task["rules_fp"])
    phrase_n = int(ruleset["phrase_n"])

    # Document-level diagnostics always cover the whole snapshot: a missing
    # revision outside the sample is just as much of a problem.
    missing_revisions = [doc_id for doc_id, info in full_snapshot.items()
                         if info["phrases"] is None]
    incomplete = [doc_id for doc_id, info in full_snapshot.items()
                  if info["phrases"] is not None and not info["token_count"]
                  and info["phrases"] == []]

    phrase_sets = {d: info["phrases"] for d, info in snapshot.items()
                   if info["phrases"] is not None}
    identical: dict[str, list[str]] = {}
    for doc_id, info in snapshot.items():
        identical.setdefault(info["content_hash"], []).append(doc_id)

    stored = {}
    for edge in store.db.execute(
            "SELECT * FROM similarity_edges WHERE task_id=?", (task_id,)).fetchall():
        if edge["doc_a"] in snapshot and edge["doc_b"] in snapshot:
            stored[(edge["doc_a"], edge["doc_b"])] = edge

    truth = indexing.exhaustive_pairs(phrase_sets, threshold, identical)
    truth_by_pair = {(t["doc_a"], t["doc_b"]): t for t in truth}

    missing = []
    extra = []
    mismatched = []
    for pair, t in truth_by_pair.items():
        edge = stored.get(pair)
        if t["above"] and edge is None:
            missing.append({"doc_a": pair[0], "doc_b": pair[1],
                            "jaccard": t["jaccard"], "title_a": snapshot[pair[0]]["title"],
                            "title_b": snapshot[pair[1]]["title"]})
        elif not t["above"] and edge is not None:
            extra.append({"edge_id": edge["id"], "doc_a": pair[0], "doc_b": pair[1],
                          "stored": edge["jaccard"], "recomputed": t["jaccard"]})
        elif edge is not None and abs(edge["jaccard"] - t["jaccard"]) > 1e-9:
            mismatched.append({"edge_id": edge["id"], "doc_a": pair[0],
                               "doc_b": pair[1], "stored": edge["jaccard"],
                               "recomputed": t["jaccard"]})
    for pair, edge in stored.items():
        if pair not in truth_by_pair:
            extra.append({"edge_id": edge["id"], "doc_a": pair[0], "doc_b": pair[1],
                          "stored": edge["jaccard"], "recomputed": None})

    pairs_total = len(truth_by_pair)
    above_truth = sum(1 for t in truth if t["above"])
    exact_mode = task["task_mode"] == "exact"
    verdict = "pass" if not missing and not extra and not mismatched else "fail"
    if task["task_mode"] == "approx" and missing:
        verdict = "approx-incomplete"
    return {
        "task_id": task_id,
        "mode": task["task_mode"],
        "threshold": threshold,
        "phrase_n": phrase_n,
        "rules_fp": task["rules_fp"],
        "scope": scope,
        "documents": len(snapshot),
        "documents_total": total_docs,
        "pairs_compared": pairs_total,
        "pairs_above_threshold": above_truth,
        "stored_edges": len(stored),
        "missing_revisions": missing_revisions,
        "empty_documents": incomplete,
        "missing": missing[:200],
        "missing_total": len(missing),
        "extra": extra[:200],
        "extra_total": len(extra),
        "score_mismatch": mismatched[:200],
        "score_mismatch_total": len(mismatched),
        "verdict": verdict,
        "completeness_claim": (
            ("exact: every pair among the %d sampled documents was compared and the "
             "candidate filters are provably lossless for those pairs; this does not "
             "certify pairs outside the sample." % len(snapshot))
            if exact_mode and scope == "sample" else
            "exact: every pair was compared and the candidate filters are provably "
            "lossless; a pass means no above-threshold pair is missing."
            if exact_mode else
            "approx: candidates came from MinHash/LSH and may be incomplete; "
            "a pass only means the stored edges are correct, not that all pairs "
            "were found."),
    }


def verify_pair(store: Store, task_id: str, doc_a: str, doc_b: str) -> dict[str, Any]:
    """Recompute one pair independently from the original text."""
    task = store.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    ruleset = store.get_ruleset(task["rules_fp"])
    doc_a_row = store.get_document(doc_a)
    doc_b_row = store.get_document(doc_b)
    if doc_a_row is None or doc_b_row is None:
        raise KeyError("unknown document")
    phrase_n = int(ruleset["phrase_n"])
    toks_a = textrules.tokenize_spans(doc_a_row["text"])
    toks_b = textrules.tokenize_spans(doc_b_row["text"])
    set_a = {textrules.phrase_key(p) for p, _c in
             textrules.slice_phrases_spans(toks_a, phrase_n)}
    set_b = {textrules.phrase_key(p) for p, _c in
             textrules.slice_phrases_spans(toks_b, phrase_n)}
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    score = inter / union if union else 0.0
    stripped_a = [t.text for t in toks_a]
    stripped_b = [t.text for t in toks_b]
    same_chars = len(doc_a_row["text"]) == len(doc_b_row["text"]) and \
        doc_a_row["content_hash"] == doc_b_row["content_hash"]
    if same_chars:
        score = 1.0
    lo, hi = sorted((len(set_a), len(set_b)))
    return {
        "doc_a": doc_a, "doc_b": doc_b,
        "jaccard": score, "intersection": inter, "union_size": union,
        "phrase_count_a": len(set_a), "phrase_count_b": len(set_b),
        "token_count_a": len(stripped_a), "token_count_b": len(stripped_b),
        "identical_text": same_chars,
        "length_filter_would_admit": (lo / hi >= float(task["threshold"]))
        if hi else False,
        "prefix_length_a": indexing.prefix_length(len(set_a), float(task["threshold"])),
        "prefix_length_b": indexing.prefix_length(len(set_b), float(task["threshold"])),
        "threshold": task["threshold"],
        "above_threshold": score >= float(task["threshold"]),
    }
