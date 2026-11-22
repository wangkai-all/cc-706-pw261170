"""Deterministic index building and exact similarity.

Everything here is pure computation over an explicit snapshot of documents: the
same inputs produce the same edges, and the app layer decides when a snapshot is
taken and committed.

Pipeline for one task
---------------------
1. Snapshot: active documents ordered by creation time then id. The set of
   (doc id, content hash) is recorded on the task, so a later upload makes the
   task visibly stale instead of silently incomplete.
2. Revisions: tokenize and slice phrases for each document, storing tokens
   with character spans and the deduplicated phrase set. Revisions are keyed by
   the rule-set fingerprint: changing any rule creates new revisions and a new
   task; old revisions are never reused.
3. Vocabulary: the global term order for the snapshot (document frequency
   ascending, then term key). Its digest is stored on the task. A different
   snapshot or rule set produces a different digest.
4. Candidate generation, three evidence-backed exact filters (none can drop a
   pair whose true Jaccard is >= threshold):
   * identical-text: equal content hashes always score 1.0;
   * length filter: for |A| <= |B|, Jaccard <= |A|/|B|;
   * prefix index (AllPairs / positional filter) over the global term order:
     every true match shares an indexed element among the first
     ``|S| - ceil(t*|S|) + 1`` positions of each set.
   Candidate pairs must pass the prefix filter *and* the length filter.
5. Exact Jaccard is computed for every candidate pair from the full phrase
   sets; only pairs at or above the threshold are stored as edges, together
   with intersection, union and the filter reason that surfaced them.

An optional approximate mode (MinHash + banded LSH) is provided for large
corpora. It is labelled ``approx-candidate`` on every edge and in every view;
it can miss pairs, so it never claims completeness and it is never the default.

The worker is resumable: each revision is committed individually and the task
row records progress. A stop request aborts between documents; re-running the
task continues from committed revisions.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from . import textrules
from .db import Row
from .store import Store, now


# ---------------------------------------------------------------------------
# Filters and exact scoring
# ---------------------------------------------------------------------------

def length_compatible(len_a: int, len_b: int, threshold: float) -> bool:
    """Exact Jaccard length bound: |A∩B| <= min and |A∪B| >= max."""
    if len_a <= 0 or len_b <= 0:
        return False
    lo, hi = (len_a, len_b) if len_a <= len_b else (len_b, len_a)
    return lo / hi >= threshold


def prefix_length(size: int, threshold: float) -> int:
    """AllPairs prefix size for a set of ``size`` elements."""
    if size <= 0:
        return 0
    overlap = math.ceil(threshold * size)
    return max(1, size - overlap + 1)


def jaccard_from_sets(a: Iterable[str], b: Iterable[str]) -> tuple[float, int, int]:
    sa, sb = set(a), set(b)
    inter = len(sa & sb)
    union = len(sa | sb)
    return (inter / union if union else 0.0), inter, union


def stable_sorted_docs(docs: Iterable[str]) -> list[str]:
    return sorted(docs)


def build_term_order(phrase_sets: dict[str, list[str]]) -> list[tuple[str, int, int]]:
    """Return ``[(term, doc_freq, rank)]`` in the global vocabulary order."""
    df: dict[str, int] = {}
    for phrases in phrase_sets.values():
        for p in set(phrases):
            df[p] = df.get(p, 0) + 1
    ordered = sorted(df.items(), key=lambda kv: (kv[1], kv[0]))
    return [(term, freq, rank) for rank, (term, freq) in enumerate(ordered)]


def vocabulary_digest(entries: Iterable[tuple[str, int, int]]) -> str:
    h = hashlib.sha256()
    for term, freq, rank in entries:
        h.update(("%d\x1f%s\n" % (rank, term)).encode("utf-8"))
    return h.hexdigest()[:24]


def candidate_pairs_exact(
    phrase_sets: dict[str, list[str]],
    threshold: float,
    term_order: Sequence[str] | None = None,
) -> dict[tuple[str, str], str]:
    """Exact candidate generation over phrase sets (empty sets produce nothing).

    Returns ``{(doc_a, doc_b): reason}`` with ``doc_a < doc_b``. Every pair whose
    true Jaccard is >= threshold is present; false positives are expected and
    removed by the exact scoring step.

    One lossless filter, the AllPairs prefix index:

    * a true match with Jaccard >= t shares at least
      ``ceil(t*|S|)`` elements with each of its two sets, so it must share one
      of the first ``|S| - ceil(t*|S|) + 1`` elements of each set;
    * sets are ordered by the global vocabulary order (rare terms first), so
      that prefix holds the most selective terms;
    * every pair sharing a prefix term is re-checked with the length bound
      ``min/max >= t``, which is also lossless.

    Lower thresholds widen the prefix toward the whole set, so candidate volume
    grows (a threshold of 0.5 on near-random documents can admit most pairs).
    That is a cost, not a correctness problem: the exact scoring step and
    full-pairwise verification still decide what counts as a match.
    """
    docs = sorted(d for d, p in phrase_sets.items() if p)
    if len(docs) < 2:
        return {}
    order = list(term_order) if term_order is not None else \
        [t for t, _f, _r in build_term_order(phrase_sets)]
    rank = {term: i for i, term in enumerate(order)}
    sets = {d: set(phrase_sets[d]) for d in docs}
    sizes = {d: len(sets[d]) for d in docs}

    reasons: dict[tuple[str, str], str] = {}
    prefix_index: dict[str, list[str]] = {}
    for doc in docs:
        ranked = sorted(sets[doc], key=lambda p: (rank.get(p, len(rank)), p))
        for term in ranked[:prefix_length(len(ranked), threshold)]:
            prefix_index.setdefault(term, []).append(doc)
    for term in sorted(prefix_index):
        for a, b in itertools.combinations(prefix_index[term], 2):
            if not length_compatible(sizes[a], sizes[b], threshold):
                continue
            pair = (a, b) if a < b else (b, a)
            reasons.setdefault(pair, "prefix+length")
    return reasons



def candidate_pairs_minhash(
    phrase_sets: dict[str, list[str]], threshold: float, num_perm: int = 128,
) -> tuple[dict[tuple[str, str], str], dict[tuple[str, str], float]]:
    """Approximate candidates via MinHash + banded LSH.

    This is a probabilistic filter: it can miss true matches. The returned
    pairs are only candidates; scores are always recomputed exactly.
    """
    docs = sorted(d for d, p in phrase_sets.items() if p)
    if len(docs) < 2:
        return {}, {}
    num_perm = max(16, min(num_perm, 512))
    bands = max(1, min(num_perm, int(round(math.sqrt(threshold * num_perm))) or 1))
    rows = max(1, num_perm // bands)
    used = bands * rows
    mask = (1 << 64) - 1
    primes = (4294967311, 4294967357, 4294967371, 4294967377, 4294967387)
    sigs: dict[str, list[int]] = {}
    for doc in docs:
        hashes = [int.from_bytes(hashlib.blake2b(t.encode("utf-8"), digest_size=8).digest(), "big")
                  for t in set(phrase_sets[doc])]
        sig = []
        for i in range(used):
            a = (0x9E3779B97F4A7C15 + i * 0xBF58476D1CE4E5B9) & mask
            b = (primes[i % len(primes)] + i) & mask
            sig.append(min((a * h + b) & mask for h in hashes))
        sigs[doc] = sig
    buckets: dict[tuple[int, int], list[str]] = {}
    for doc in docs:
        sig = sigs[doc]
        for band in range(bands):
            key = (band, hash(tuple(sig[band * rows:(band + 1) * rows])))
            buckets.setdefault(key, []).append(doc)
    pairs: dict[tuple[str, str], str] = {}
    for key in sorted(buckets):
        for a, b in itertools.combinations(sorted(buckets[key]), 2):
            pairs.setdefault((a, b), "minhash:band%d" % key[0])
    estimates = {}
    for pair in pairs:
        a, b = pair
        same = sum(1 for x, y in zip(sigs[a], sigs[b]) if x == y)
        estimates[pair] = same / used
    return pairs, estimates


# ---------------------------------------------------------------------------
# Task options and worker
# ---------------------------------------------------------------------------

@dataclass
class BuildOptions:
    threshold: float = 0.5
    phrase_n: int = textrules.DEFAULT_PHRASE_N
    tokenizer_version: str = textrules.TOKENIZER_ID
    phrase_version: str = textrules.PHRASES_ID
    vocab_version: str = textrules.VOCAB_ID
    mode: str = "exact"          # 'exact' | 'approx'
    max_docs: int = 20000
    num_perm: int = 128

    def validate(self) -> None:
        if not 0.0 < self.threshold <= 1.0:
            raise ValueError("threshold must be in (0, 1]")
        if not textrules.MIN_PHRASE_N <= self.phrase_n <= textrules.MAX_PHRASE_N:
            raise ValueError("phrase_n out of range")
        if self.mode not in ("exact", "approx"):
            raise ValueError("mode must be exact or approx")
        if self.tokenizer_version != textrules.TOKENIZER_ID:
            raise ValueError("unknown tokenizer version: %s" % self.tokenizer_version)
        if self.phrase_version != textrules.PHRASES_ID:
            raise ValueError("unknown phrase version: %s" % self.phrase_version)
        if self.vocab_version != textrules.VOCAB_ID:
            raise ValueError("unknown vocabulary version: %s" % self.vocab_version)

    @classmethod
    def from_task(cls, ruleset: Row, task: Row) -> "BuildOptions":
        return cls(
            threshold=float(task["threshold"]),
            phrase_n=int(ruleset["phrase_n"]),
            tokenizer_version=ruleset["tokenizer_version"],
            phrase_version=ruleset["phrase_version"],
            vocab_version=ruleset["vocab_version"],
            mode=task["task_mode"],
        )


@dataclass
class Progress:
    task_id: str
    phase: str
    done: int
    total: int
    message: str = ""
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id, "phase": self.phase, "done": self.done,
            "total": self.total, "message": self.message, "errors": self.errors,
        }


class IndexWorker:
    """Step-wise, resumable index builder.

    Every commit boundary is a safe stopping point. ``stop`` may be flipped
    from another thread to abort between documents; the task is left
    ``stopped`` and a later run continues from the committed revisions.
    """

    def __init__(self, store: Store, task_id: str, options: BuildOptions,
                 stop: Callable[[], bool] | None = None,
                 progress: Callable[[Progress], None] | None = None):
        self.store = store
        self.db = store.db
        self.task_id = task_id
        self.options = options
        self.stop = stop or (lambda: False)
        self.progress = progress

    def _emit(self, phase: str, done: int, total: int, message: str = "") -> Progress:
        p = Progress(self.task_id, phase, done, total, message)
        if self.progress:
            self.progress(p)
        return p

    def _snapshot(self) -> list[Row]:
        return self.db.execute(
            "SELECT * FROM documents WHERE withdrawn_at IS NULL"
            " ORDER BY created_at, id").fetchall()

    def run(self) -> Progress:
        opts = self.options
        opts.validate()
        fp = self.store.ensure_ruleset(opts.tokenizer_version, opts.phrase_version,
                                       opts.phrase_n, opts.vocab_version)
        task = self.store.get_task(self.task_id)
        if task is None:
            raise KeyError(self.task_id)
        if task["rules_fp"] != fp:
            raise ValueError("task %s belongs to rule set %s, not %s"
                             % (self.task_id, task["rules_fp"], fp))

        docs = self._snapshot()
        if len(docs) > opts.max_docs:
            raise ValueError("snapshot has %d documents; max_docs is %d"
                             % (len(docs), opts.max_docs))
        total = len(docs)
        snapshot = {d["id"]: d["content_hash"] for d in docs}
        self.store.update_task(self.task_id, status="running", total=total,
                               started_at=now(), last_error=None,
                               snapshot_json=json.dumps(snapshot, sort_keys=True))
        self.db.commit()

        # -- phase 1: revisions ------------------------------------------
        done = 0
        for doc in docs:
            if self.stop():
                self.store.update_task(self.task_id, status="stopped", done=done)
                self.db.commit()
                return self._emit("stopped", done, total, "stopped before all revisions")
            existing = self.store.revision_for(doc["id"], fp)
            if existing is None or existing["content_hash"] != doc["content_hash"]:
                self.store.ensure_revision(doc["id"], fp, opts.phrase_n)
            done += 1
            self.store.update_task(self.task_id, done=done)
            self.db.commit()
        self._emit("revisions", done, total)

        # -- phase 2: vocabulary -----------------------------------------
        revs = self.db.execute(
            "SELECT r.* FROM doc_revisions r JOIN documents d ON d.id=r.doc_id"
            " WHERE r.rules_fp=? AND d.withdrawn_at IS NULL"
            " ORDER BY d.created_at, d.id", (fp,)).fetchall()
        phrase_sets = {r["doc_id"]: [k for k, _c in json.loads(r["phrases_json"])]
                       for r in revs}
        entries = build_term_order(phrase_sets)
        digest = vocabulary_digest(entries)
        self.db.execute("DELETE FROM vocabulary WHERE rules_fp=?", (fp,))
        self.db.executemany(
            "INSERT INTO vocabulary(rules_fp,term,doc_freq,rank) VALUES(?,?,?,?)",
            [(fp, t, f, r) for t, f, r in entries])
        self.store.update_task(self.task_id, note="vocab=%s" % digest,
                               vocab_digest=digest)
        self.db.commit()
        self._emit("vocabulary", done, total, "vocabulary digest %s" % digest)

        # -- phase 3: candidates + exact edges ---------------------------
        self.db.execute("DELETE FROM similarity_edges WHERE task_id=?", (self.task_id,))
        if opts.mode == "approx":
            cand, estimates = candidate_pairs_minhash(phrase_sets, opts.threshold,
                                                      opts.num_perm)
            provenance = "approx-candidate"
        else:
            cand = candidate_pairs_exact(phrase_sets, opts.threshold,
                                         [t for t, _f, _r in entries])
            estimates = {}
            provenance = "candidate"

        # Identical text is an exact finding in either mode. Empty documents
        # carry no phrases and are deliberately not treated as similar to each
        # other: two empty texts are both empty, not duplicates of content.
        hash_groups: dict[str, list[str]] = {}
        for rev in revs:
            if rev["token_count"] > 0:
                hash_groups.setdefault(rev["content_hash"], []).append(rev["doc_id"])
        identical_pairs: dict[tuple[str, str], str] = {}
        for ids in hash_groups.values():
            for a, b in itertools.combinations(sorted(ids), 2):
                identical_pairs[(a, b)] = "identical-hash"

        rows = []
        for (a, b) in sorted(set(cand) | set(identical_pairs)):
            phrase_a = phrase_sets.get(a, [])
            phrase_b = phrase_sets.get(b, [])
            sim, inter, union = jaccard_from_sets(phrase_a, phrase_b)
            reason = identical_pairs.get((a, b)) or cand.get((a, b), "")
            if (a, b) in identical_pairs:
                sim = 1.0
            if sim < opts.threshold:
                continue
            rows.append((self.task_id, fp, a, b, sim, inter, union, provenance,
                         reason, estimates.get((a, b)), now()))
        self.db.executemany(
            "INSERT INTO similarity_edges(task_id,rules_fp,doc_a,doc_b,jaccard,"
            "intersection,union_size,provenance,candidate_reason,estimated_jaccard,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
        empty = sum(1 for r in revs if not r["token_count"])
        short = sum(1 for r in revs if r["token_count"] and not r["phrase_count"])
        identical = sum(1 for (a, b) in identical_pairs
                        if (a, b) in {(r[2], r[3]) for r in rows})
        note = ("mode=%s vocab=%s candidates=%d edges=%d identical_text_edges=%d"
                " empty=%d short=%d" % (opts.mode, digest, len(cand), len(rows),
                                        identical, empty, short))
        self.store.update_task(self.task_id, status="completed", done=done,
                               finished_at=now(), note=note)
        self.db.commit()
        return self._emit("completed", done, total, note)


def exhaustive_pairs(phrase_sets: dict[str, list[str]], threshold: float,
                     identical: dict[str, list[str]] | None = None) -> list[dict]:
    """Every pair compared directly; used to verify the index on small samples.

    ``identical`` maps content hash -> doc ids so identical text still scores
    1.0 even when the phrase sets are empty because the text is shorter than n.
    """
    identical_pairs: set[tuple[str, str]] = set()
    for ids in (identical or {}).values():
        for a, b in itertools.combinations(sorted(ids), 2):
            identical_pairs.add((a, b))
    out = []
    docs = sorted(phrase_sets)
    for i, a in enumerate(docs):
        for b in docs[i + 1:]:
            pair = (a, b)
            sim, inter, union = jaccard_from_sets(phrase_sets[a], phrase_sets[b])
            if pair in identical_pairs:
                sim = 1.0
            out.append({
                "doc_a": a, "doc_b": b, "jaccard": sim,
                "intersection": inter, "union_size": union,
                "above": sim >= threshold,
            })
    return out
