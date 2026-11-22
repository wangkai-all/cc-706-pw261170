"""Pair inspection: aligned shared fragments and shared phrase evidence.

The score of a pair is the Jaccard index over the deduplicated phrase sets of
the two revisions under one rule-set fingerprint. This module never changes
that score; it only explains it.

Alignment
---------
The UI needs "the same fragment side by side", not just a number. We run a
token-level LCS (``difflib.SequenceMatcher``) over the two ordered token
streams. Each matching block becomes a segment with an original character span
in both documents. Blocks separated by very short gaps are merged, and a
segment is tagged with the shared phrases it fully contains. The Jaccard score
is computed from the phrase *sets*, so alignment and score can legitimately
differ (e.g. the same words in a different order); both are shown so that is
visible rather than hidden.
"""

from __future__ import annotations

import difflib
import json
from dataclasses import dataclass
from typing import Any

from . import textrules
from .db import Row
from .store import Store


MAX_ALIGN_TOKENS = 40000
MIN_SEGMENT_TOKENS = 2
MAX_MERGE_GAP = 2


def load_revision(store: Store, doc_id: str, rules_fp: str) -> Row | None:
    return store.revision_for(doc_id, rules_fp)


def _tokens(rev: Row) -> list[tuple[str, int, int, bool]]:
    return [tuple(row) for row in json.loads(rev["tokens_json"])]


def _phrases(rev: Row) -> dict[str, int]:
    return {key: count for key, count in json.loads(rev["phrases_json"])}


def _span(tokens: list[tuple[str, int, int, bool]], i: int, j: int) -> tuple[int, int]:
    return tokens[i][1], tokens[j - 1][2]


def _segment_tokens(tokens: list[tuple[str, int, int, bool]], i: int, j: int) -> list[str]:
    return [t[0] for t in tokens[i:j]]


def _shared_phrase_hits(seg: list[str], shared: set[str], n: int) -> list[str]:
    if n <= 0 or len(seg) < n:
        return []
    hits: list[str] = []
    for i in range(len(seg) - n + 1):
        key = textrules.phrase_key(seg[i:i + n])
        if key in shared:
            hits.append(key)
    return hits


def _first_phrase_spans(tokens: list[tuple[str, int, int, bool]], n: int) -> dict[str, tuple[int, int]]:
    """Map phrase key -> first (start, end) character span in this document."""
    spans: dict[str, tuple[int, int]] = {}
    if n <= 0 or len(tokens) < n:
        return spans
    for i in range(len(tokens) - n + 1):
        if any(tokens[j][3] for j in range(i, i + n - 1)):
            continue
        key = textrules.phrase_key([t[0] for t in tokens[i:i + n]])
        if key not in spans:
            spans[key] = (tokens[i][1], tokens[i + n - 1][2])
    return spans


def align_documents(doc_a: Row, doc_b: Row, rev_a: Row, rev_b: Row,
                    phrase_n: int, max_shared: int = 400) -> dict[str, Any]:
    tokens_a = _tokens(rev_a)
    tokens_b = _tokens(rev_b)
    phrases_a = _phrases(rev_a)
    phrases_b = _phrases(rev_b)
    shared_keys = set(phrases_a) & set(phrases_b)
    only_a = sorted(set(phrases_a) - set(phrases_b))
    only_b = sorted(set(phrases_b) - set(phrases_a))
    spans_a = _first_phrase_spans(tokens_a, phrase_n)
    spans_b = _first_phrase_spans(tokens_b, phrase_n)

    alignable = len(tokens_a) <= MAX_ALIGN_TOKENS and len(tokens_b) <= MAX_ALIGN_TOKENS
    segments: list[dict[str, Any]] = []
    if alignable and tokens_a and tokens_b:
        matcher = difflib.SequenceMatcher(
            None, [t[0] for t in tokens_a], [t[0] for t in tokens_b], autojunk=False)
        raw = [m for m in matcher.get_matching_blocks() if m.size > 0]
        merged: list[list[int]] = []
        for m in raw:
            if merged and m.a - (merged[-1][0] + merged[-1][2]) <= MAX_MERGE_GAP \
                    and m.b - (merged[-1][1] + merged[-1][2]) <= MAX_MERGE_GAP:
                prev = merged[-1]
                prev[2] = (m.a + m.size) - prev[0]
            else:
                merged.append([m.a, m.b, m.size])
        for a0, b0, size in merged:
            if size < MIN_SEGMENT_TOKENS and not shared_keys:
                continue
            seg_tokens = _segment_tokens(tokens_a, a0, a0 + size)
            hits = _shared_phrase_hits(seg_tokens, shared_keys, phrase_n)
            a_start, a_end = _span(tokens_a, a0, a0 + size)
            b_start, b_end = _span(tokens_b, b0, b0 + size)
            segments.append({
                "index": len(segments),
                "a": {"start": a_start, "end": a_end,
                      "text": doc_a["text"][a_start:a_end], "token_count": size},
                "b": {"start": b_start, "end": b_end,
                      "text": doc_b["text"][b_start:b_end], "token_count": size},
                "shared_phrases": [textrules.phrase_key_display(k)
                                   for k in sorted(set(hits))[:24]],
                "shared_phrase_count": len(set(hits)),
                "is_full_text": (size == len(tokens_a) == len(tokens_b)),
            })
    elif alignable and not tokens_a and not tokens_b:
        segments = []

    return {
        "doc_a": public_pair_doc(doc_a, rev_a),
        "doc_b": public_pair_doc(doc_b, rev_b),
        "rules": {"tokenizer_version": rev_a["rules_fp"], "phrase_n": phrase_n,
                  "jaccard_definition": (
                      "|phrases(A) ∩ phrases(B)| / |phrases(A) ∪ phrases(B)| "
                      "over deduplicated phrase sets")},
        "alignment": {
            "available": alignable,
            "reason": None if alignable else (
                "documents exceed %d tokens; use the shared phrase list instead"
                % MAX_ALIGN_TOKENS),
            "segments": segments,
            "note": ("Segments are token-level identical runs with short gaps merged. "
                     "The Jaccard score is over phrase sets and may differ from the "
                     "visible aligned text when order changes."),
        },
        "shared_phrases": [
            {"key": k, "display": textrules.phrase_key_display(k),
             "count_a": phrases_a[k], "count_b": phrases_b[k],
             "span_a": spans_a.get(k) and {"start": spans_a[k][0], "end": spans_a[k][1]},
             "span_b": spans_b.get(k) and {"start": spans_b[k][0], "end": spans_b[k][1]}}
            for k in sorted(shared_keys, key=lambda k: (-(phrases_a[k] + phrases_b[k]), k))[:max_shared]
        ],
        "shared_phrase_total": len(shared_keys),
        "only_in_a": {"total": len(only_a),
                      "sample": [textrules.phrase_key_display(k) for k in only_a[:80]]},
        "only_in_b": {"total": len(only_b),
                      "sample": [textrules.phrase_key_display(k) for k in only_b[:80]]},
        "phrase_counts": {"a": len(phrases_a), "b": len(phrases_b),
                          "intersection": len(shared_keys),
                          "union": len(set(phrases_a) | set(phrases_b))},
    }


def public_pair_doc(row: Row, rev: Row | None) -> dict[str, Any]:
    return {
        "id": row["id"], "title": row["title"], "status": row["status"],
        "withdrawn_at": row["withdrawn_at"], "withdrawn_reason": row["withdrawn_reason"],
        "source": row["source"], "source_ref": row["source_ref"],
        "content_hash": row["content_hash"], "char_count": row["char_count"],
        "token_count": rev["token_count"] if rev is not None else row["token_count"],
        "phrase_count": rev["phrase_count"] if rev is not None else row["phrase_count"],
        "text": row["text"],
        "rules_fp": rev["rules_fp"] if rev is not None else None,
        "revision_created_at": rev["created_at"] if rev is not None else None,
    }


def task_edge(store: Store, edge_id: int) -> Row | None:
    return store.db.execute(
        "SELECT e.*, COALESCE(s.decision,'unreviewed') AS decision, s.note AS decision_note"
        " FROM similarity_edges e LEFT JOIN edge_state s ON s.task_id=e.task_id"
        "   AND s.doc_a=e.doc_a AND s.doc_b=e.doc_b"
        " WHERE e.id=?", (edge_id,)).fetchone()
