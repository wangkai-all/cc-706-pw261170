"""HTTP application: JSON API plus the single-page review UI.

Implementation notes
--------------------
* Stdlib only (``http.server`` + our SQLite wrapper), because the environment
  has no third-party packages available. The server is threaded so a running
  index task never blocks the UI.
* Every request opens its own :class:`Store`; SQLite WAL lets the background
  index worker write while requests read.
* Background index workers are tracked in-process. On startup, tasks left in
  ``running`` are marked ``stopped``; their committed revisions remain, and
  ``POST /api/tasks/<id>/run`` resumes from there.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import exports, grouping, indexing, ingest, similarity, textrules, verification
from .store import Store, now

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DEFAULT_DB = BASE_DIR.parent / "data" / "corpuslake.db"
# How long a resume waits for a stopped worker to finish committing and exit.
STOP_HANDSHAKE_SECONDS = 30.0

ROUTES = []


def route(method: str, pattern: str):
    regex = re.compile("^" + pattern + "$")

    def deco(fn):
        ROUTES.append((method, regex, fn))
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, status: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


class RawResponse:
    """A non-JSON response (CSV, N-Quads, zip) with explicit headers."""

    __slots__ = ("payload", "content_type", "headers")

    def __init__(self, payload, content_type: str, headers: dict | None = None):
        self.payload = payload
        self.content_type = content_type
        self.headers = headers or {}

    def render(self):
        data = self.payload.encode("utf-8") if isinstance(self.payload, str) else self.payload
        return data, self.content_type, self.headers



class AppContext:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._workers: dict[str, "WorkerHandle"] = {}
        self._progress: dict[str, dict] = {}
        self._mark_interrupted()

    def store(self) -> Store:
        return Store(self.db_path)

    def _mark_interrupted(self) -> None:
        store = self.store()
        try:
            for task in store.list_tasks():
                if task["status"] in ("running", "queued"):
                    store.update_task(task["id"], status="stopped",
                                      last_error=None,
                                      note="interrupted by server restart; revision "
                                           "progress is kept, resume to continue")
            store.db.commit()
        finally:
            store.close()

    # -- worker management -------------------------------------------
    def start_task_run(self, task_id: str) -> dict:
        for _attempt in range(2):
            with self._lock:
                handle = self._workers.get(task_id)
                if handle is None or not handle.thread.is_alive():
                    break
                if not handle.stop_event.is_set():
                    return {"started": False, "reason": "already running"}
                if handle.thread is threading.current_thread():
                    return {"started": False, "reason": "already running"}
            # A stop was requested but the old worker is still finishing its
            # last document. Wait *outside* the lock so other requests are not
            # blocked, then retry: the resume must never overlap with the run
            # it is meant to replace.
            handle.thread.join(timeout=STOP_HANDSHAKE_SECONDS)
        with self._lock:
            handle = self._workers.get(task_id)
            if handle is not None and handle.thread.is_alive():
                return {"started": False, "reason": "stopping",
                        "detail": "the previous run is still committing; retry "
                                  "in a moment"}
            store = self.store()
            task = store.get_task(task_id)
            if task is None:
                store.close()
                raise KeyError(task_id)
            ruleset = store.get_ruleset(task["rules_fp"])
            options = indexing.BuildOptions.from_task(ruleset, task)
            # Reset any run-specific state here, before the worker overwrites
            # it, so the API can never show a previous run's progress.
            store.update_task(task_id, status="queued", done=0, last_error=None,
                              note=None, finished_at=None)
            store.db.commit()
            store.close()
            stop_event = threading.Event()

            def progress_cb(p: indexing.Progress) -> None:
                self._progress[task_id] = p.as_dict()

            def work() -> None:
                worker_store = self.store()
                try:
                    worker = indexing.IndexWorker(
                        worker_store, task_id, options,
                        stop=stop_event.is_set, progress=progress_cb)
                    result = worker.run()
                    self._progress[task_id] = result.as_dict()
                    if result.phase == "completed":
                        grouping.rebuild_groups(worker_store, task_id)
                        self._progress[task_id]["groups_rebuilt"] = True
                except Exception as exc:  # noqa: BLE001 - surface in UI
                    self._progress[task_id] = {
                        "task_id": task_id, "phase": "failed", "done": 0, "total": 0,
                        "message": "%s: %s" % (type(exc).__name__, exc),
                        "trace": traceback.format_exc()[-4000:],
                    }
                    try:
                        worker_store.update_task(task_id, status="failed",
                                                 last_error=str(exc),
                                                 finished_at=now())
                        worker_store.db.commit()
                    except Exception:  # noqa: BLE001
                        pass
                finally:
                    worker_store.close()

            thread = threading.Thread(target=work, name="index-%s" % task_id,
                                      daemon=True)
            handle = WorkerHandle(task_id, thread, stop_event)
            self._workers[task_id] = handle
            thread.start()
            return {"started": True, "task_id": task_id}

    def stop_task(self, task_id: str) -> dict:
        with self._lock:
            handle = self._workers.get(task_id)
        if handle is not None and handle.thread.is_alive():
            handle.stop_event.set()
            return {"stopping": True, "task_id": task_id}
        store = self.store()
        try:
            task = store.get_task(task_id)
            if task is None:
                raise KeyError(task_id)
            if task["status"] in ("running", "queued"):
                store.update_task(task_id, status="stopped")
                store.db.commit()
            return {"stopping": False, "task_id": task_id,
                    "status": store.get_task(task_id)["status"]}
        finally:
            store.close()

    def progress(self, task_id: str) -> dict | None:
        return self._progress.get(task_id)


class WorkerHandle:
    __slots__ = ("task_id", "thread", "stop_event")

    def __init__(self, task_id: str, thread: threading.Thread, stop_event: threading.Event):
        self.task_id = task_id
        self.thread = thread
        self.stop_event = stop_event


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _json_default(value):
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length") or 0)
    if length <= 0:
        return {}
    if length > 32 * 1024 * 1024:
        raise ApiError(413, "request body too large (32 MB limit)")
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApiError(400, "invalid JSON body: %s" % exc) from None
    if not isinstance(data, dict):
        raise ApiError(400, "JSON body must be an object")
    return data


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _task_payload(store: Store, task, progress: dict | None) -> dict:
    ruleset = store.get_ruleset(task["rules_fp"])
    edge_count = store.db.execute(
        "SELECT COUNT(*) AS n FROM similarity_edges WHERE task_id=?",
        (task["id"],)).fetchone()["n"]
    unreviewed = store.db.execute(
        "SELECT COUNT(*) AS n FROM similarity_edges e LEFT JOIN edge_state s"
        " ON s.task_id=e.task_id AND s.doc_a=e.doc_a AND s.doc_b=e.doc_b"
        " WHERE e.task_id=? AND COALESCE(s.decision,'unreviewed')='unreviewed'",
        (task["id"],)).fetchone()["n"]
    snapshot = json.loads(task["snapshot_json"]) if task["snapshot_json"] else None
    active_hashes = {r["id"]: r["content_hash"] for r in store.db.execute(
        "SELECT id, content_hash FROM documents WHERE withdrawn_at IS NULL").fetchall()}
    snapshot_current = None
    if snapshot is not None:
        snapshot_current = all(active_hashes.get(doc_id) == h
                               for doc_id, h in snapshot.items())
        snapshot_current = snapshot_current and set(active_hashes) == set(snapshot)
    return {
        "id": task["id"], "status": task["status"], "mode": task["task_mode"],
        "threshold": task["threshold"], "total": task["total"], "done": task["done"],
        "rules_fp": task["rules_fp"], "note": task["note"],
        "started_at": task["started_at"], "finished_at": task["finished_at"],
        "last_error": task["last_error"], "vocab_digest": task["vocab_digest"],
        "snapshot": snapshot, "snapshot_current": snapshot_current,
        "rules": textrules.describe_rules(
            ruleset["tokenizer_version"], ruleset["phrase_version"],
            int(ruleset["phrase_n"]), ruleset["vocab_version"]),
        "edge_count": edge_count, "unreviewed_edges": unreviewed,
        "progress": progress,
        "approximate": task["task_mode"] == "approx",
        "completeness": (
            "exact mode: prefix + length filters are lossless, every pair was "
            "compared, so the edge list is complete for this snapshot and rule set."
            if task["task_mode"] == "exact" else
            "APPROXIMATE mode: MinHash/LSH candidates can miss pairs. Edges shown "
            "are exact scores, but the list is not claimed to be complete."),
    }


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@route("GET", r"/api/health")
def api_health(ctx: AppContext, match, query, body):
    return {"ok": True, "time": now(), "version": "1.0.0"}


@route("GET", r"/api/rules")
def api_rules(ctx: AppContext, match, query, body):
    n = _int(query.get("phrase_n", [textrules.DEFAULT_PHRASE_N])[0],
             textrules.DEFAULT_PHRASE_N)
    return {
        "rules": textrules.describe_rules(
            textrules.TOKENIZER_ID, textrules.PHRASES_ID, n),
        "supported_n": list(range(textrules.MIN_PHRASE_N, textrules.MAX_PHRASE_N + 1)),
        "default_n": textrules.DEFAULT_PHRASE_N,
    }


@route("GET", r"/api/stats")
def api_stats(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        docs = store.db.execute(
            "SELECT COUNT(*) AS n, SUM(is_empty) AS empty,"
            " SUM(CASE WHEN withdrawn_at IS NOT NULL THEN 1 ELSE 0 END) AS withdrawn"
            " FROM documents").fetchone()
        task = store.latest_task()
        edges = groups = 0
        if task is not None:
            edges = store.db.execute(
                "SELECT COUNT(*) AS n FROM similarity_edges WHERE task_id=?",
                (task["id"],)).fetchone()["n"]
            groups = store.db.execute(
                "SELECT COUNT(*) AS n FROM groups WHERE task_id=?",
                (task["id"],)).fetchone()["n"]
        return {
            "documents": docs["n"] or 0, "empty_documents": docs["empty"] or 0,
            "withdrawn": docs["withdrawn"] or 0,
            "task": _task_payload(store, task, ctx.progress(task["id"]))
            if task is not None else None,
            "edges": edges, "groups": groups,
            "db_path": ctx.db_path,
        }
    finally:
        store.close()


@route("GET", r"/api/documents")
def api_documents(ctx: AppContext, match, query, body):
    include = query.get("include_withdrawn", ["1"])[0] not in ("0", "false", "")
    store = ctx.store()
    try:
        rows = store.list_documents(include_withdrawn=include)
        return {"documents": [grouping.dict_public_doc(r) for r in rows]}
    finally:
        store.close()


@route("POST", r"/api/documents")
def api_add_document(ctx: AppContext, match, query, body):
    title = str(body.get("title") or "").strip()
    text = body.get("text")
    if not isinstance(text, str):
        raise ApiError(400, "field 'text' is required and must be a string")
    duplicate_of = body.get("duplicate_of")
    store = ctx.store()
    try:
        if duplicate_of:
            target = store.get_document(str(duplicate_of))
            if target is None:
                raise ApiError(404, "duplicate_of document not found")
        else:
            existing = store.find_identical(text)
            if existing is not None:
                return (HTTPStatus.CONFLICT, {
                    "identical": True,
                    "existing": grouping.dict_public_doc(existing),
                    "message": ("This text is already stored under the same content "
                                "hash. Open the existing document, or store this "
                                "upload as a separate identity by sending "
                                "duplicate_of."),
                })
        row, _created = store.add_document(
            title or "Untitled", text, source="upload",
            source_ref=body.get("source_ref"),
            duplicate_of=str(duplicate_of) if duplicate_of else None)
        return (HTTPStatus.CREATED, {"document": grouping.dict_public_doc(row)})
    finally:
        store.close()


@route("GET", r"/api/documents/([A-Za-z0-9_]+)")
def api_document(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        row = store.get_document(match.group(1))
        if row is None:
            raise ApiError(404, "document not found")
        data = grouping.dict_public_doc(row)
        data["text"] = row["text"]
        revision = store.db.execute(
            "SELECT * FROM doc_revisions WHERE doc_id=? ORDER BY id DESC LIMIT 1",
            (row["id"],)).fetchone()
        data["revision"] = None if revision is None else {
            "rules_fp": revision["rules_fp"], "phrase_n": revision["phrase_n"],
            "token_count": revision["token_count"],
            "phrase_count": revision["phrase_count"],
            "created_at": revision["created_at"],
        }
        return data
    finally:
        store.close()


@route("POST", r"/api/documents/([A-Za-z0-9_]+)/withdraw")
def api_withdraw(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        doc_id = match.group(1)
        reason = str(body.get("reason") or "")
        result = store.withdraw_document(doc_id, reason)
        rebuilds = grouping.rebuild_for_document(store, doc_id)
        return {"withdraw": result,
                "group_rebuilds": rebuilds,
                "group_rebuild": rebuilds[0] if rebuilds else None}
    finally:
        store.close()


@route("POST", r"/api/documents/([A-Za-z0-9_]+)/restore")
def api_restore(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        doc_id = match.group(1)
        if store.get_document(doc_id) is None:
            raise ApiError(404, "document not found")
        store.restore_document(doc_id)
        rebuilds = grouping.rebuild_for_document(store, doc_id)
        return {"restored": doc_id, "group_rebuilds": rebuilds,
                "group_rebuild": rebuilds[0] if rebuilds else None}
    finally:
        store.close()


@route("GET", r"/api/tasks")
def api_tasks(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        tasks = [_task_payload(store, t, ctx.progress(t["id"]))
                 for t in store.list_tasks()]
        return {"tasks": tasks}
    finally:
        store.close()


@route("POST", r"/api/tasks")
def api_create_task(ctx: AppContext, match, query, body):
    threshold = _float(body.get("threshold", 0.6), 0.6)
    phrase_n = _int(body.get("phrase_n", textrules.DEFAULT_PHRASE_N),
                    textrules.DEFAULT_PHRASE_N)
    mode = str(body.get("mode") or "exact")
    num_perm = _int(body.get("num_perm", 128), 128)
    options = indexing.BuildOptions(threshold=threshold, phrase_n=phrase_n, mode=mode,
                                    num_perm=num_perm)
    options.validate()
    store = ctx.store()
    try:
        fp = store.ensure_ruleset(options.tokenizer_version, options.phrase_version,
                                  options.phrase_n, options.vocab_version)
        task = store.create_task(fp, mode, threshold,
                                 note="created from UI, n=%d" % phrase_n)
        data = _task_payload(store, task, None)
    finally:
        store.close()
    started = ctx.start_task_run(task["id"])
    return (HTTPStatus.CREATED, {"task": data, "started": started})


@route("GET", r"/api/tasks/([A-Za-z0-9_]+)")
def api_task(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        task = store.get_task(match.group(1))
        if task is None:
            raise ApiError(404, "task not found")
        return _task_payload(store, task, ctx.progress(task["id"]))
    finally:
        store.close()


@route("POST", r"/api/tasks/([A-Za-z0-9_]+)/run")
def api_run_task(ctx: AppContext, match, query, body):
    task_id = match.group(1)
    store = ctx.store()
    try:
        task = store.get_task(task_id)
        if task is None:
            raise ApiError(404, "task not found")
        if body.get("threshold") is not None:
            raise ApiError(400, "threshold is fixed at task creation; create a new task "
                                "when changing rules or threshold")
        same_ruleset = store.get_ruleset(task["rules_fp"])
        current = indexing.BuildOptions(
            threshold=task["threshold"],
            phrase_n=int(same_ruleset["phrase_n"]),
            tokenizer_version=same_ruleset["tokenizer_version"],
            phrase_version=same_ruleset["phrase_version"],
            vocab_version=same_ruleset["vocab_version"],
            mode=task["task_mode"])
        current.validate()
    finally:
        store.close()
    return ctx.start_task_run(task_id)


@route("POST", r"/api/tasks/([A-Za-z0-9_]+)/stop")
def api_stop_task(ctx: AppContext, match, query, body):
    return ctx.stop_task(match.group(1))


@route("GET", r"/api/tasks/([A-Za-z0-9_]+)/edges")
def api_task_edges(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        task = store.get_task(match.group(1))
        if task is None:
            raise ApiError(404, "task not found")
        min_score = _float(query.get("min_score", ["0"])[0], 0.0)
        decision = query.get("decision", [""])[0]
        doc_id = query.get("doc", [""])[0]
        limit = max(1, min(_int(query.get("limit", ["500"])[0], 500), 2000))
        rows = grouping.all_edges(store, task["id"])
        docs = {d["id"]: d for d in store.list_documents(include_withdrawn=True)}
        out = []
        for e in rows:
            if e["jaccard"] + 1e-12 < min_score:
                continue
            if decision and e["decision"] != decision:
                continue
            if doc_id and doc_id not in (e["doc_a"], e["doc_b"]):
                continue
            item = grouping.edge_public(e)
            item["doc_a_title"] = docs[e["doc_a"]]["title"] if e["doc_a"] in docs else "?"
            item["doc_b_title"] = docs[e["doc_b"]]["title"] if e["doc_b"] in docs else "?"
            item["doc_a_withdrawn"] = bool(docs[e["doc_a"]]["withdrawn_at"]) \
                if e["doc_a"] in docs else True
            item["doc_b_withdrawn"] = bool(docs[e["doc_b"]]["withdrawn_at"]) \
                if e["doc_b"] in docs else True
            out.append(item)
        return {"task": _task_payload(store, task, ctx.progress(task["id"])),
                "edges": out[:limit], "total": len(out)}
    finally:
        store.close()


@route("GET", r"/api/edges/([0-9]+)")
def api_edge_detail(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        edge = similarity.task_edge(store, int(match.group(1)))
        if edge is None:
            raise ApiError(404, "edge not found")
        doc_a = store.get_document(edge["doc_a"])
        doc_b = store.get_document(edge["doc_b"])
        rev_a = store.revision_for(doc_a["id"], edge["rules_fp"]) if doc_a else None
        rev_b = store.revision_for(doc_b["id"], edge["rules_fp"]) if doc_b else None
        if doc_a is None or doc_b is None:
            raise ApiError(404, "a document of this edge no longer exists")
        if rev_a is None or rev_b is None:
            raise ApiError(409,
                           "revisions for this edge are missing: the index was built "
                           "under another rule set or a document changed; re-run the "
                           "task to regenerate exact comparison detail")
        ruleset = store.get_ruleset(edge["rules_fp"])
        detail = similarity.align_documents(doc_a, doc_b, rev_a, rev_b,
                                            int(ruleset["phrase_n"]))
        task = store.get_task(edge["task_id"])
        group_rows = store.db.execute(
            "SELECT g.id, g.label FROM groups g JOIN group_members gm ON gm.group_id=g.id"
            " WHERE g.task_id=? AND gm.doc_id=? ORDER BY g.created_at", (edge["task_id"], doc_a["id"])).fetchall()
        groups_a = [dict(r) for r in group_rows]
        group_shared = [g for g in groups_a if store.db.execute(
            "SELECT 1 FROM group_members WHERE group_id=? AND doc_id=?",
            (g["id"], doc_b["id"])).fetchone() is not None]
        detail["edge"] = grouping.edge_public(edge)
        if "decision_note" in edge.keys():
            detail["edge"]["decision_note"] = edge["decision_note"]
        detail["task"] = {
            "id": task["id"], "mode": task["task_mode"], "threshold": task["threshold"],
            "rules_fp": task["rules_fp"], "approximate": task["task_mode"] == "approx",
        }
        same_group_members = 0
        if group_shared:
            ids = [g["id"] for g in group_shared]
            same_group_members = len({m["doc_id"] for m in store.db.execute(
                "SELECT doc_id FROM group_members WHERE group_id IN (%s)"
                % ",".join("?" * len(ids)), ids).fetchall()})
        detail["group_context"] = {
            "groups_of_a": groups_a,
            "groups_shared": group_shared,
            "same_group_members": same_group_members,
            "note": ("This edge's two documents are in the same connected-component "
                     "group (%d documents). That is a component, not a clique: other "
                     "member pairs may have no direct edge or score below the "
                     "threshold." % same_group_members)
            if group_shared else
            ("These two documents are not in the same group. A chain of edges may "
             "still connect them elsewhere; the group view shows components."),
        }
        detail["independent_check"] = verification.verify_pair(
            store, task["id"], doc_a["id"], doc_b["id"])
        return detail
    finally:
        store.close()


@route("POST", r"/api/edges/([0-9]+)/decision")
def api_edge_decision(ctx: AppContext, match, query, body):
    edge_id = int(match.group(1))
    decision = str(body.get("decision") or "")
    note = str(body.get("note") or "")
    store = ctx.store()
    try:
        edge = store.db.execute("SELECT * FROM similarity_edges WHERE id=?",
                                (edge_id,)).fetchone()
        if edge is None:
            raise ApiError(404, "edge not found")
        grouping.set_edge_decision(store, edge_id, decision, note)
        report = grouping.rebuild_groups(store, edge["task_id"]).as_dict()
        return {"edge_id": edge_id, "decision": decision, "group_rebuild": report}
    finally:
        store.close()


@route("GET", r"/api/tasks/([A-Za-z0-9_]+)/groups")
def api_groups(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        task = store.get_task(match.group(1))
        if task is None:
            raise ApiError(404, "task not found")
        groups = grouping.group_detail_rows(store, task["id"])
        runs = [{"run_id": r["id"], "created_at": r["created_at"],
                 "algorithm": r["algorithm"], "note": r["note"],
                 "splits": json.loads(r["splits_json"])}
                for r in grouping.group_runs(store, task["id"])]
        return {"task_id": task["id"], "groups": groups, "runs": runs,
                "note": ("A group is a connected component of edges, not a "
                         "clique. Pairs without a direct edge are listed per group.")}
    finally:
        store.close()


@route("POST", r"/api/tasks/([A-Za-z0-9_]+)/groups/rebuild")
def api_groups_rebuild(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        task = store.get_task(match.group(1))
        if task is None:
            raise ApiError(404, "task not found")
        return grouping.rebuild_groups(store, task["id"]).as_dict()
    finally:
        store.close()


@route("GET", r"/api/groups/([A-Za-z0-9_]+)")
def api_group_detail(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        detail = grouping.group_detail(store, match.group(1))
        if detail is None:
            raise ApiError(404, "group not found")
        return detail
    finally:
        store.close()


@route("POST", r"/api/tasks/([A-Za-z0-9_]+)/verify")
def api_verify(ctx: AppContext, match, query, body):
    store = ctx.store()
    try:
        task = store.get_task(match.group(1))
        if task is None:
            raise ApiError(404, "task not found")
        body = body or {}
        full = bool(body.get("full", False))
        try:
            sample_size = int(body.get("sample_size") or verification.MAX_VERIFY_DOCS)
        except (TypeError, ValueError):
            raise ApiError(400, "sample_size must be an integer") from None
        if sample_size < 2:
            raise ApiError(400, "sample_size must be at least 2")
        try:
            return verification.verify_task(store, task["id"],
                                            sample_size=sample_size, full=full)
        except ValueError as exc:
            raise ApiError(400, str(exc)) from None
    finally:
        store.close()


@route("GET", r"/api/tasks/([A-Za-z0-9_]+)/export")
def api_export(ctx: AppContext, match, query, body):
    fmt = query.get("format", ["bundle"])[0]
    store = ctx.store()
    try:
        task = store.get_task(match.group(1))
        if task is None:
            raise ApiError(404, "task not found")
        if fmt == "manifest":
            return exports.corpus_manifest(store, task["id"])
        if fmt == "groups":
            return exports.groups_document(store, task["id"])
        if fmt == "edges.csv":
            return RawResponse(exports.edges_csv(store, task["id"]),
                               "text/csv; charset=utf-8",
                               {"Content-Disposition": 'attachment; filename="edges.csv"'})
        if fmt == "links.nq":
            return RawResponse(exports.links_nquads(store, task["id"]),
                               "application/n-quads; charset=utf-8",
                               {"Content-Disposition": 'attachment; filename="links.nq"'})
        dest = Path(ctx.db_path).parent / "exports"
        path = exports.export_bundle(store, dest, task["id"])
        return RawResponse(path.read_bytes(), "application/zip",
                           {"Content-Disposition": 'attachment; filename="%s"' % path.name})
    finally:
        store.close()


@route("POST", r"/api/import")
def api_import(ctx: AppContext, match, query, body):
    path = body.get("path")
    if not isinstance(path, str) or not path:
        raise ApiError(400, "field 'path' is required")
    target = Path(path).expanduser()
    store = ctx.store()
    try:
        if target.is_dir():
            result = ingest.import_directory(store, target)
        elif target.is_file():
            report = ingest.import_file(store, target)
            result = {"imported": report.imported, "skipped": report.skipped,
                      "errors": report.errors,
                      "files": [{"file": str(target), "imported": report.imported,
                                 "skipped": report.skipped,
                                 "errors": len(report.errors)}]}
        else:
            raise ApiError(404, "path not found: %s" % path)
        return result
    finally:
        store.close()


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "CorpusLake/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        if not getattr(self.server, "quiet", False):
            print("%s - %s" % (self.address_string(), fmt % args))

    def _send(self, status: int, payload, content_type="application/json; charset=utf-8",
              headers: dict | None = None) -> None:
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        ctx: AppContext = self.server.ctx  # type: ignore[attr-defined]
        try:
            if path == "/" or path.startswith("/index.html"):
                return self._serve_static("index.html")
            if path.startswith("/static/"):
                return self._serve_static(path[len("/static/"):])
            body = _read_json(self) if method in ("POST", "PUT", "PATCH") else {}
            for route_method, regex, fn in ROUTES:
                if route_method != method:
                    continue
                m = regex.match(path)
                if m is None:
                    continue
                result = fn(ctx, m, query, body)
                if isinstance(result, RawResponse):
                    data, ctype, headers = result.render()
                    return self._send(200, data, content_type=ctype, headers=headers)
                if isinstance(result, tuple):
                    status, payload = result[0], result[1]
                    return self._send(status, payload)
                return self._send(200, result)
            return self._send(404, {"error": "not found", "path": path})
        except ApiError as exc:
            payload = {"error": exc.message}
            payload.update(exc.extra)
            return self._send(exc.status, payload)
        except KeyError as exc:
            return self._send(404, {"error": "not found: %s" % exc})
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001 - API boundary
            traceback.print_exc()
            return self._send(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    def _serve_static(self, rel: str) -> None:
        rel = rel.split("?")[0]
        if rel in ("", "index.html"):
            path = STATIC_DIR / "index.html"
        else:
            path = (STATIC_DIR / rel).resolve()
            if not str(path).startswith(str(STATIC_DIR.resolve())):
                return self._send(403, {"error": "forbidden"})
        if not path.is_file():
            return self._send(404, {"error": "not found", "path": rel})
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send(200, path.read_bytes(), content_type=ctype)

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")


class CorpusServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, ctx: AppContext, quiet: bool = False):
        self.ctx = ctx
        self.quiet = quiet
        super().__init__(address, Handler)


def serve(host: str = "127.0.0.1", port: int = 8765, db_path: str | Path = DEFAULT_DB) -> None:
    ctx = AppContext(db_path)
    server = CorpusServer((host, port), ctx)
    print("corpuslake serving on http://%s:%d  (db: %s)" % (host, port, db_path))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        server.server_close()


def make_test_server(db_path: str | Path, host: str = "127.0.0.1", port: int = 0):
    ctx = AppContext(db_path)
    server = CorpusServer((host, port), ctx, quiet=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
