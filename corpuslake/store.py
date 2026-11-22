"""Schema and document lifecycle for the corpus lake.

Design notes
------------
* Documents are append-only identities. "Withdrawing" a document stamps
  ``withdrawn_at`` and keeps the original text; nothing is deleted in place.
* Each (document, content hash, rule set) gets a ``doc_revisions`` row holding
  the token and phrase artefacts with character spans. An index task is a
  named, resumable build over the active snapshot.
* Similarity edges store the exact pair value and the provenance of the
  candidate filter that surfaced them. Edges are *not* expanded to cliques:
  similarity is not transitive and every edge stays a first-class row.
* Groups are a review decision over edges, persisted per group, and rebuilt
  from the surviving edges after a withdrawal.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from . import textrules
from .db import Database, Row

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rulesets (
    fingerprint TEXT PRIMARY KEY,
    tokenizer_version TEXT NOT NULL,
    phrase_version TEXT NOT NULL,
    phrase_n INTEGER NOT NULL,
    vocab_version TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'upload',
    source_ref TEXT,
    content_hash TEXT NOT NULL,
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    phrase_count INTEGER NOT NULL DEFAULT 0,
    is_empty INTEGER NOT NULL DEFAULT 0,
    duplicate_of TEXT,
    created_at REAL NOT NULL,
    withdrawn_at REAL,
    withdrawn_reason TEXT,
    status TEXT NOT NULL DEFAULT 'active'
);
CREATE INDEX IF NOT EXISTS idx_documents_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);

CREATE TABLE IF NOT EXISTS doc_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    rules_fp TEXT NOT NULL REFERENCES rulesets(fingerprint),
    phrase_n INTEGER NOT NULL,
    token_count INTEGER NOT NULL,
    phrase_count INTEGER NOT NULL,
    tokens_json TEXT NOT NULL,
    phrases_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(doc_id, content_hash, rules_fp)
);
CREATE INDEX IF NOT EXISTS idx_rev_rules ON doc_revisions(rules_fp);

CREATE TABLE IF NOT EXISTS vocabulary (
    rules_fp TEXT NOT NULL REFERENCES rulesets(fingerprint),
    term TEXT NOT NULL,
    doc_freq INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    PRIMARY KEY (rules_fp, term)
);
CREATE INDEX IF NOT EXISTS idx_vocab_rank ON vocabulary(rules_fp, rank);

CREATE TABLE IF NOT EXISTS index_tasks (
    id TEXT PRIMARY KEY,
    rules_fp TEXT NOT NULL REFERENCES rulesets(fingerprint),
    task_mode TEXT NOT NULL DEFAULT 'exact',
    status TEXT NOT NULL,
    threshold REAL NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    done INTEGER NOT NULL DEFAULT 0,
    snapshot_json TEXT,
    vocab_digest TEXT,
    started_at REAL,
    finished_at REAL,
    last_error TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS similarity_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES index_tasks(id) ON DELETE CASCADE,
    rules_fp TEXT NOT NULL,
    doc_a TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    doc_b TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    jaccard REAL NOT NULL,
    intersection INTEGER NOT NULL,
    union_size INTEGER NOT NULL,
    provenance TEXT NOT NULL,          -- 'candidate' | 'approx-candidate'
    candidate_reason TEXT,
    estimated_jaccard REAL,
    created_at REAL NOT NULL,
    CHECK (doc_a < doc_b),
    UNIQUE(task_id, doc_a, doc_b)
);
CREATE INDEX IF NOT EXISTS idx_edges_task ON similarity_edges(task_id, jaccard);
CREATE INDEX IF NOT EXISTS idx_edges_a ON similarity_edges(doc_a);
CREATE INDEX IF NOT EXISTS idx_edges_b ON similarity_edges(doc_b);

CREATE TABLE IF NOT EXISTS edge_state (
    task_id TEXT NOT NULL REFERENCES index_tasks(id) ON DELETE CASCADE,
    doc_a TEXT NOT NULL,
    doc_b TEXT NOT NULL,
    decision TEXT NOT NULL,            -- 'unreviewed' | 'same_group' | 'keep_separate'
    decided_at REAL,
    note TEXT,
    PRIMARY KEY (task_id, doc_a, doc_b),
    CHECK (doc_a < doc_b)
);

CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES index_tasks(id) ON DELETE CASCADE,
    label TEXT,
    algorithm TEXT NOT NULL,
    run_id TEXT,
    created_at REAL NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_groups_task ON groups(task_id);

CREATE TABLE IF NOT EXISTS group_runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES index_tasks(id) ON DELETE CASCADE,
    algorithm TEXT NOT NULL,
    created_at REAL NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    splits_json TEXT NOT NULL,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_group_runs_task ON group_runs(task_id, created_at);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    added_at REAL NOT NULL,
    PRIMARY KEY (group_id, doc_id)
);

CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


def now() -> float:
    return time.time()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:12])


class Store:
    def __init__(self, db_path: str | Path):
        self.db = Database(db_path)
        self.db.executescript(SCHEMA)
        self._ensure_meta()

    def close(self) -> None:
        self.db.close()

    def _ensure_meta(self) -> None:
        row = self.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("schema_version", "1"))
            self.db.execute("INSERT INTO meta(key,value) VALUES(?,?)", ("created_at", str(now())))
            self.db.commit()

    # -- rulesets ------------------------------------------------------
    def ensure_ruleset(self, tokenizer_version: str, phrase_version: str,
                       phrase_n: int, vocab_version: str) -> str:
        fp = textrules.ruleset_fingerprint(tokenizer_version, phrase_version,
                                           phrase_n, vocab_version)
        row = self.db.execute("SELECT 1 FROM rulesets WHERE fingerprint=?", (fp,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO rulesets(fingerprint, tokenizer_version, phrase_version,"
                " phrase_n, vocab_version, created_at) VALUES(?,?,?,?,?,?)",
                (fp, tokenizer_version, phrase_version, phrase_n, vocab_version, now()))
            self.db.commit()
        return fp

    def get_ruleset(self, fp: str) -> Row | None:
        return self.db.execute("SELECT * FROM rulesets WHERE fingerprint=?", (fp,)).fetchone()

    def list_rulesets(self) -> list[Row]:
        return self.db.execute("SELECT * FROM rulesets ORDER BY created_at DESC").fetchall()

    # -- documents -----------------------------------------------------
    def add_document(self, title: str, text: str, source: str = "upload",
                     source_ref: str | None = None,
                     duplicate_of: str | None = None) -> tuple[Row, bool]:
        """Insert a document; returns (row, created).

        Identical text is *not* silently merged. The caller decides: the web
        upload reports the existing document and offers "open it" or "store as
        a separate identity" (which passes ``duplicate_of``). This keeps the
        identity of every upload explicit while making the duplicate visible.
        """
        chash = content_hash(text)
        tokens = textrules.tokenize_spans(text)
        doc_id = new_id("doc")
        self.db.execute(
            "INSERT INTO documents(id,title,source,source_ref,content_hash,text,"
            "char_count,token_count,phrase_count,is_empty,duplicate_of,created_at,status)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'active')",
            (doc_id, title.strip() or "Untitled", source, source_ref, chash, text,
             len(text), len(tokens), 0, 1 if not tokens else 0, duplicate_of, now()))
        self._event("document_added", {"doc_id": doc_id, "hash": chash,
                                       "title": title, "duplicate_of": duplicate_of})
        self.db.commit()
        row = self.get_document(doc_id)
        assert row is not None
        return row, True

    def find_identical(self, text: str) -> Row | None:
        return self.db.execute(
            "SELECT * FROM documents WHERE content_hash=? AND withdrawn_at IS NULL"
            " ORDER BY created_at LIMIT 1", (content_hash(text),)).fetchone()

    def get_document(self, doc_id: str) -> Row | None:
        return self.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()

    def list_documents(self, include_withdrawn: bool = True) -> list[Row]:
        if include_withdrawn:
            return self.db.execute(
                "SELECT * FROM documents ORDER BY created_at DESC").fetchall()
        return self.db.execute(
            "SELECT * FROM documents WHERE withdrawn_at IS NULL"
            " ORDER BY created_at DESC").fetchall()

    def withdraw_document(self, doc_id: str, reason: str = "") -> dict[str, Any]:
        doc = self.get_document(doc_id)
        if doc is None:
            raise KeyError(doc_id)
        if doc["withdrawn_at"] is not None:
            return {"doc_id": doc_id, "already_withdrawn": True}
        self.db.execute(
            "UPDATE documents SET withdrawn_at=?, withdrawn_reason=?, status='withdrawn'"
            " WHERE id=?", (now(), reason, doc_id))
        self._event("document_withdrawn", {"doc_id": doc_id, "reason": reason})
        self.db.commit()
        return {"doc_id": doc_id, "already_withdrawn": False}

    def restore_document(self, doc_id: str) -> None:
        self.db.execute(
            "UPDATE documents SET withdrawn_at=NULL, withdrawn_reason=NULL, status='active'"
            " WHERE id=?", (doc_id,))
        self._event("document_restored", {"doc_id": doc_id})
        self.db.commit()

    # -- revisions -----------------------------------------------------
    def revision_for(self, doc_id: str, rules_fp: str) -> Row | None:
        return self.db.execute(
            "SELECT * FROM doc_revisions WHERE doc_id=? AND rules_fp=?"
            " ORDER BY id DESC LIMIT 1", (doc_id, rules_fp)).fetchone()

    def ensure_revision(self, doc_id: str, rules_fp: str, phrase_n: int) -> Row:
        doc = self.get_document(doc_id)
        if doc is None:
            raise KeyError(doc_id)
        existing = self.revision_for(doc_id, rules_fp)
        if existing is not None and existing["content_hash"] == doc["content_hash"]:
            return existing
        tokens = textrules.tokenize_spans(doc["text"])
        phrases = textrules.slice_phrases_spans(tokens, phrase_n)
        cur = self.db.execute(
            "INSERT INTO doc_revisions(doc_id,content_hash,rules_fp,phrase_n,"
            "token_count,phrase_count,tokens_json,phrases_json,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            (doc_id, doc["content_hash"], rules_fp, phrase_n, len(tokens),
             len(phrases),
             json.dumps([t.as_row() for t in tokens], ensure_ascii=False),
             json.dumps([[p, c] for p, c in phrases], ensure_ascii=False), now()))
        rev_id = cur.lastrowid
        self.db.execute(
            "UPDATE documents SET token_count=?, phrase_count=?, is_empty=? WHERE id=?",
            (len(tokens), len(phrases), 1 if not tokens else 0, doc_id))
        return self.db.execute("SELECT * FROM doc_revisions WHERE id=?", (rev_id,)).fetchone()

    # -- tasks ---------------------------------------------------------
    def create_task(self, rules_fp: str, mode: str, threshold: float,
                    note: str = "") -> Row:
        task_id = new_id("task")
        self.db.execute(
            "INSERT INTO index_tasks(id,rules_fp,task_mode,status,threshold,started_at,note)"
            " VALUES(?,?,?,'queued',?,?,?)",
            (task_id, rules_fp, mode, threshold, now(), note))
        self._event("task_created", {"task_id": task_id, "rules_fp": rules_fp, "mode": mode})
        self.db.commit()
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> Row | None:
        return self.db.execute("SELECT * FROM index_tasks WHERE id=?", (task_id,)).fetchone()

    def latest_task(self) -> Row | None:
        return self.db.execute(
            "SELECT * FROM index_tasks WHERE status='completed'"
            " ORDER BY finished_at DESC, started_at DESC LIMIT 1").fetchone()

    def list_tasks(self) -> list[Row]:
        return self.db.execute(
            "SELECT * FROM index_tasks ORDER BY started_at DESC").fetchall()

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {"status", "total", "done", "finished_at", "last_error", "note",
                   "snapshot_json", "vocab_digest", "started_at"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise ValueError(k)
            sets.append("%s=?" % k)
            params.append(v)
        params.append(task_id)
        self.db.execute("UPDATE index_tasks SET %s WHERE id=?" % ",".join(sets), params)

    # -- events --------------------------------------------------------
    def _event(self, event: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO review_events(at,event,payload_json) VALUES(?,?,?)",
            (now(), event, json.dumps(payload, ensure_ascii=False, sort_keys=True)))

    def event(self, event: str, payload: dict[str, Any]) -> None:
        """Public event recorder (commits)."""
        self._event(event, payload)
        self.db.commit()
