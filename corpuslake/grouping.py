"""Grouping over similarity edges - explicitly *not* a clique.

Similarity is not transitive: A~B and B~C does not imply A~C. A group here is
the connected component of *accepted* edges for one task. It is a navigation
aid, not a claim that every pair in the group clears the threshold. The UI and
the exports always list the underlying edges and every pair that has no direct
edge, so the group view cannot be mistaken for an all-pairs assertion.

Accepted edges are all edges at or above the task threshold whose review
decision is not ``keep_separate``. Marking an edge ``keep_separate`` removes
it from the graph; rebuilding then may split a group into several groups. A
withdrawn document is removed from the graph, which can likewise split a
component, dissolve it, or leave a singleton that becomes independent.

Every rebuild is recorded as a *run* with the before/after membership so a
split is visible after the fact.
"""

from __future__ import annotations

import itertools
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .db import Row
from .store import Store, now


def _run_id() -> str:
    return "grun_" + uuid.uuid4().hex[:12]


def _group_id() -> str:
    return "grp_" + uuid.uuid4().hex[:12]


@dataclass
class GroupReport:
    task_id: str
    run_id: str
    groups: int
    members: int
    basis: str = "accepted"
    independent: list[str] = field(default_factory=list)
    splits: list[dict[str, Any]] = field(default_factory=list)
    merged: list[dict[str, Any]] = field(default_factory=list)
    dissolved: list[dict[str, Any]] = field(default_factory=list)
    withdrawn_removed: list[dict[str, Any]] = field(default_factory=list)
    accepted_edges: int = 0
    excluded_edges: int = 0

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id, "run_id": self.run_id,
            "groups": self.groups, "basis": self.basis,
            "members": self.members,
            "independent": self.independent, "splits": self.splits,
            "merged": self.merged,
            "dissolved": self.dissolved,
            "withdrawn_removed": self.withdrawn_removed,
            "accepted_edges": self.accepted_edges,
            "excluded_edges": self.excluded_edges,
        }


def accepted_edges(store: Store, task_id: str, basis: str = "accepted") -> list[Row]:
    """Edges usable for grouping.

    ``basis='accepted'`` (default): every edge at/above threshold that has not
    been marked ``keep_separate``. ``basis='confirmed'``: only edges explicitly
    marked ``same_group``. Both exclude edges touching a withdrawn document.
    """
    if basis not in ("accepted", "confirmed"):
        raise ValueError("unknown grouping basis: %s" % basis)
    rows = store.db.execute(
        "SELECT e.*, COALESCE(s.decision,'unreviewed') AS decision,"
        "       s.note AS decision_note"
        " FROM similarity_edges e"
        " JOIN documents da ON da.id=e.doc_a"
        " JOIN documents db ON db.id=e.doc_b"
        " LEFT JOIN edge_state s ON s.task_id=e.task_id"
        "   AND s.doc_a=e.doc_a AND s.doc_b=e.doc_b"
        " WHERE e.task_id=? AND da.withdrawn_at IS NULL AND db.withdrawn_at IS NULL"
        " ORDER BY e.jaccard DESC, e.doc_a, e.doc_b", (task_id,)).fetchall()
    if basis == "confirmed":
        return [r for r in rows if r["decision"] == "same_group"]
    return [r for r in rows if r["decision"] != "keep_separate"]



def all_edges(store: Store, task_id: str) -> list[Row]:
    return store.db.execute(
        "SELECT e.*, COALESCE(s.decision,'unreviewed') AS decision,"
        "       s.note AS decision_note"
        " FROM similarity_edges e"
        " LEFT JOIN edge_state s ON s.task_id=e.task_id"
        "   AND s.doc_a=e.doc_a AND s.doc_b=e.doc_b"
        " WHERE e.task_id=?"
        " ORDER BY e.jaccard DESC, e.doc_a, e.doc_b", (task_id,)).fetchall()


def _components(nodes: Iterable[str], edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for a, b in edges:
        if a in parent and b in parent:
            union(a, b)
    buckets: dict[str, list[str]] = {}
    for n in parent:
        buckets.setdefault(find(n), []).append(n)
    return [sorted(members) for members in buckets.values()]


def rebuild_groups(store: Store, task_id: str,
                   basis: str = "accepted") -> GroupReport:
    """Rebuild all groups of ``task_id`` from accepted edges (transactional).

    ``basis`` selects which edges count as accepted; see :func:`accepted_edges`.
    """
    db = store.db
    task = store.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    docs = db.execute(
        "SELECT id FROM documents WHERE withdrawn_at IS NULL ORDER BY id").fetchall()
    nodes = [r["id"] for r in docs]
    edges = accepted_edges(store, task_id, basis)
    all_rows = all_edges(store, task_id)
    excluded = len(all_rows) - len(edges)

    old_rows = db.execute(
        "SELECT g.id AS group_id, gm.doc_id FROM groups g"
        " JOIN group_members gm ON gm.group_id=g.id"
        " WHERE g.task_id=? ORDER BY g.id", (task_id,)).fetchall()
    before: dict[str, list[str]] = {}
    for row in old_rows:
        before.setdefault(row["group_id"], []).append(row["doc_id"])
    node_set = set(nodes)
    before_active = {gid: sorted(d for d in members if d in node_set)
                     for gid, members in before.items()}
    withdrawn_removed = [
        {"old_group": gid, "withdrawn_members": sorted(d for d in members if d not in node_set)}
        for gid, members in before.items()
        if any(d not in node_set for d in members)
    ]

    comps = [c for c in _components(nodes, [(e["doc_a"], e["doc_b"]) for e in edges])
             if len(c) > 1]
    comps.sort(key=lambda c: (c[0], len(c)))
    grouped = {d for c in comps for d in c}
    independent = sorted(set(nodes) - grouped)

    run_id = _run_id()
    algorithm = "connected-components:%s" % basis
    after: dict[str, list[str]] = {}
    created: dict[str, str] = {}
    try:
        db.begin()
        db.execute("DELETE FROM groups WHERE task_id=?", (task_id,))
        for members in comps:
            gid = _group_id()
            created[gid] = members
            after[gid] = members
            db.execute(
                "INSERT INTO groups(id,task_id,label,algorithm,created_at,run_id)"
                " VALUES(?,?,?,?,?,?)",
                (gid, task_id, "Group %d" % (len(created)), algorithm, now(), run_id))
            db.executemany(
                "INSERT INTO group_members(group_id,doc_id,added_at) VALUES(?,?,?)",
                [(gid, d, now()) for d in members])
        # A prior group can split into several groups *or* into independent
        # documents - a withdrawal can leave both halves below the threshold.
        # Singletons therefore count as landing locations for split detection.
        location_of: dict[str, str] = {}
        for gid, members in created.items():
            for d in members:
                location_of[d] = gid
        for d in independent:
            location_of[d] = "independent:%s" % d
        splits: list[dict[str, Any]] = []
        for gid, all_members in before.items():
            if len(all_members) < 2:
                continue
            active = [d for d in all_members if d in node_set]
            locations = sorted({location_of[d] for d in active if d in location_of})
            if len(locations) > 1:
                targets = []
                for loc in locations:
                    if loc.startswith("independent:"):
                        targets.append({"kind": "independent",
                                        "doc_id": loc.split(":", 1)[1],
                                        "members": [loc.split(":", 1)[1]]})
                    else:
                        targets.append({"kind": "group", "group_id": loc,
                                        "members": created[loc]})
                splits.append({
                    "old_group": gid,
                    "members": sorted(all_members),
                    "withdrawn_members": sorted(d for d in all_members
                                                if d not in node_set),
                    "new_groups": targets,
                })
        dissolved = []
        merged = []
        location_members: dict[str, set[str]] = {}
        for gid, members in before.items():
            active = [d for d in members if d in node_set]
            if not active:
                dissolved.append({"old_group": gid, "members": sorted(members)})
                continue
            for d in active:
                if d in location_of and not location_of[d].startswith("independent:"):
                    location_members.setdefault(location_of[d], set()).add(gid)
        for loc, old_groups in location_members.items():
            if len(old_groups) > 1:
                merged.append({"new_group": loc,
                               "old_groups": sorted(old_groups)})
        algorithm = "connected-components:%s" % basis
        payload = {
            "run_id": run_id, "algorithm": algorithm, "basis": basis,
            "groups": [{"group_id": g, "members": m} for g, m in created.items()],
            "independent": independent, "splits": splits, "dissolved": dissolved,
            "merged": merged,
            "withdrawn_removed": withdrawn_removed,
            "accepted_edges": [{"edge_id": e["id"], "doc_a": e["doc_a"],
                                "doc_b": e["doc_b"], "jaccard": e["jaccard"]}
                               for e in edges],
        }
        db.execute(
            "INSERT INTO group_runs(id,task_id,created_at,algorithm,before_json,"
            "after_json,splits_json,note) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, task_id, now(), algorithm,
             json.dumps(before_active, sort_keys=True),
             json.dumps(after, sort_keys=True),
             json.dumps({"splits": splits, "dissolved": dissolved,
                         "merged": merged}, sort_keys=True),
             "%d groups, %d independent, %d accepted edges" %
             (len(comps), len(independent), len(edges))))
        db.execute(
            "INSERT INTO review_events(at,event,payload_json) VALUES(?,?,?)",
            (now(), "groups_rebuilt", json.dumps(payload, ensure_ascii=False, sort_keys=True)))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return GroupReport(task_id=task_id, run_id=run_id, groups=len(comps), basis=basis,
                       members=len(grouped), independent=independent,
                       splits=splits, dissolved=dissolved, merged=merged,
                       withdrawn_removed=withdrawn_removed,
                       accepted_edges=len(edges), excluded_edges=excluded)


def group_detail(store: Store, group_id: str) -> dict[str, Any] | None:
    """Group with members, edges and *every* member pair explicitly marked.

    ``pairs`` contains all (n choose 2) pairs with ``edge`` when a direct edge
    exists and ``edge: null`` otherwise, so the client can never render the
    group as a clique by omission.
    """
    group = store.db.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    if group is None:
        return None
    rows = store.db.execute(
        "SELECT d.*, gm.added_at FROM group_members gm JOIN documents d ON d.id=gm.doc_id"
        " WHERE gm.group_id=? ORDER BY d.created_at, d.id", (group_id,)).fetchall()
    members = [dict_public_doc(r) for r in rows]
    doc_ids = sorted(r["id"] for r in rows)
    edge_rows = store.db.execute(
        "SELECT e.*, COALESCE(s.decision,'unreviewed') AS decision"
        " FROM similarity_edges e LEFT JOIN edge_state s ON s.task_id=e.task_id"
        "   AND s.doc_a=e.doc_a AND s.doc_b=e.doc_b"
        " WHERE e.task_id=? AND e.doc_a IN (%s) AND e.doc_b IN (%s)"
        % (",".join("?" * len(doc_ids)), ",".join("?" * len(doc_ids))),
        [group["task_id"]] + doc_ids + doc_ids).fetchall() if doc_ids else []
    edge_by_pair = {(e["doc_a"], e["doc_b"]): e for e in edge_rows}
    pairs = []
    for a, b in itertools.combinations(doc_ids, 2):
        e = edge_by_pair.get((a, b))
        pairs.append({
            "doc_a": a, "doc_b": b,
            "edge": None if e is None else {
                "edge_id": e["id"], "jaccard": e["jaccard"],
                "intersection": e["intersection"], "union_size": e["union_size"],
                "decision": e["decision"], "provenance": e["provenance"],
            },
        })
    run = store.db.execute("SELECT * FROM group_runs WHERE id=?",
                           (group["run_id"],)).fetchone() if group["run_id"] else None
    return {
        "group": {
            "id": group["id"], "task_id": group["task_id"], "label": group["label"],
            "algorithm": group["algorithm"], "created_at": group["created_at"],
            "run_id": group["run_id"],
        },
        "members": members,
        "edges": [edge_public(e) for e in edge_rows],
        "pairs": pairs,
        "splits": json.loads(run["splits_json"]) if run is not None else {},
        "note": ("Membership is the connected component of accepted edges. "
                 "A missing edge between two members means that pair did not "
                 "reach the threshold; the group is not a clique."),
    }


def dict_public_doc(row: Row) -> dict[str, Any]:
    return {
        "id": row["id"], "title": row["title"], "source": row["source"],
        "source_ref": row["source_ref"], "content_hash": row["content_hash"],
        "char_count": row["char_count"], "token_count": row["token_count"],
        "phrase_count": row["phrase_count"], "is_empty": bool(row["is_empty"]),
        "duplicate_of": row["duplicate_of"], "created_at": row["created_at"],
        "withdrawn_at": row["withdrawn_at"],
        "withdrawn_reason": row["withdrawn_reason"], "status": row["status"],
        "text_preview": (row["text"][:280] if row["text"] else ""),
    }


def edge_public(row: Row) -> dict[str, Any]:
    decision = row["decision"] if "decision" in row.keys() else "unreviewed"
    return {
        "edge_id": row["id"], "task_id": row["task_id"],
        "doc_a": row["doc_a"], "doc_b": row["doc_b"],
        "jaccard": row["jaccard"], "intersection": row["intersection"],
        "union_size": row["union_size"], "provenance": row["provenance"],
        "candidate_reason": row["candidate_reason"],
        "estimated_jaccard": row["estimated_jaccard"],
        "decision": decision, "created_at": row["created_at"],
    }


def set_edge_decision(store: Store, edge_id: int, decision: str, note: str = "") -> None:
    if decision not in ("unreviewed", "same_group", "keep_separate"):
        raise ValueError("unknown decision: %s" % decision)
    row = store.db.execute("SELECT * FROM similarity_edges WHERE id=?", (edge_id,)).fetchone()
    if row is None:
        raise KeyError(edge_id)
    store.db.execute(
        "INSERT INTO edge_state(task_id,doc_a,doc_b,decision,decided_at,note)"
        " VALUES(?,?,?,?,?,?)"
        " ON CONFLICT(task_id,doc_a,doc_b) DO UPDATE SET"
        " decision=excluded.decision, decided_at=excluded.decided_at, note=excluded.note",
        (row["task_id"], row["doc_a"], row["doc_b"], decision, now(), note))
    store.event("edge_decision", {"edge_id": edge_id, "decision": decision,
                                  "note": note, "doc_a": row["doc_a"],
                                  "doc_b": row["doc_b"]})


def group_runs(store: Store, task_id: str) -> list[Row]:
    return store.db.execute(
        "SELECT * FROM group_runs WHERE task_id=? ORDER BY created_at DESC",
        (task_id,)).fetchall()


def task_ids_for_document(store: Store, doc_id: str) -> list[str]:
    """Tasks whose snapshot contains ``doc_id``; those groups are affected."""
    rows = store.db.execute(
        "SELECT id, snapshot_json FROM index_tasks ORDER BY started_at", ()).fetchall()
    out = []
    for row in rows:
        if not row["snapshot_json"]:
            continue
        try:
            snapshot = json.loads(row["snapshot_json"])
        except json.JSONDecodeError:
            continue
        if doc_id in snapshot:
            out.append(row["id"])
    return out


def rebuild_for_document(store: Store, doc_id: str) -> list[dict[str, Any]]:
    """Rebuild groups for every task whose snapshot holds the document."""
    reports = []
    for task_id in task_ids_for_document(store, doc_id):
        task = store.get_task(task_id)
        if task is None:
            continue
        if task["status"] not in ("completed", "stopped"):
            continue
        reports.append(rebuild_groups(store, task_id).as_dict())
    return reports


def group_detail_rows(store: Store, task_id: str) -> list[dict[str, Any]]:
    """All groups of a task with members, edges and the explicit pair matrix."""
    ids = [r["id"] for r in store.db.execute(
        "SELECT id FROM groups WHERE task_id=? ORDER BY id", (task_id,)).fetchall()]
    out = []
    for gid in ids:
        detail = group_detail(store, gid)
        if detail is not None:
            out.append(detail)
    return out

