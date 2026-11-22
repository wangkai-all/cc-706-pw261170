"""End-to-end HTTP tests against a real threaded server.

Run: python3 -m tests.test_api
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from tests.harness import TestClient, contains, eq, make_tmp, cleanup, ok, run_cases
from corpuslake import app as app_module
from corpuslake.demo import CHAIN_A, CHAIN_B, CHAIN_C
from corpuslake.store import Store


LONG = ("Local archives must keep three copies of every record. Local archives must "
        "keep three copies of every record. Retention schedules are reviewed each "
        "year by the records committee and published in the annual report. The "
        "committee also checks that storage media remain readable.")


def _server():
    tmp = make_tmp("corpuslake_api_")
    server, thread = app_module.make_test_server(tmp / "api.db")
    port = server.server_address[1]
    return tmp, server, thread, TestClient("http://127.0.0.1:%d" % port)


def _wait_task(client, task_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, task = client.get("/api/tasks/%s" % task_id)
        eq(status, 200)
        if task["status"] in ("completed", "failed", "stopped"):
            return task
        time.sleep(0.08)
    raise AssertionError("task %s did not settle" % task_id)


def test_health_and_rules():
    tmp, server, thread, client = _server()
    try:
        status, health = client.get("/api/health")
        eq(status, 200); eq(health["ok"], True)
        status, rules = client.get("/api/rules")
        eq(status, 200)
        eq(rules["default_n"], 3)
        eq(rules["rules"]["tokenizer"]["version"], "corpuslake.tokenizer.v1")
    finally:
        server.shutdown(); cleanup(tmp)


def test_upload_identical_conflict_and_duplicate_identity():
    tmp, server, thread, client = _server()
    try:
        status, res = client.post("/api/documents", {"title": "A", "text": LONG})
        eq(status, 201)
        doc_id = res["document"]["id"]
        status, res2 = client.post("/api/documents", {"title": "A again", "text": LONG})
        eq(status, 409, "byte-identical upload is reported, not silently merged")
        eq(res2["identical"], True)
        eq(res2["existing"]["id"], doc_id)
        status, res3 = client.post("/api/documents",
                                   {"title": "A copy", "text": LONG, "duplicate_of": doc_id})
        eq(status, 201)
        ok(res3["document"]["id"] != doc_id)
        eq(res3["document"]["duplicate_of"], doc_id)
        status, docs = client.get("/api/documents")
        eq(len(docs["documents"]), 2)
    finally:
        server.shutdown(); cleanup(tmp)


def test_empty_document_accepted():
    tmp, server, thread, client = _server()
    try:
        status, res = client.post("/api/documents", {"title": "empty", "text": ""})
        eq(status, 201)
        eq(res["document"]["is_empty"], True)
        status, res2 = client.post("/api/documents", {"title": "empty2", "text": ""})
        eq(status, 409, "a second empty upload needs an explicit identity decision")
    finally:
        server.shutdown(); cleanup(tmp)


def test_task_lifecycle_and_polling():
    tmp, server, thread, client = _server()
    try:
        docs = json.loads(Path(__file__).with_name("fixtures_corpus.json").read_text()) \
            if Path(__file__).with_name("fixtures_corpus.json").exists() else None
        texts = [
            ("one", LONG),
            ("two", LONG.replace("each year", "every year")),
            ("three", "Rye sourdough needs a lively starter and a covered pot."),
            ("four", ""),
            ("five", "Short."),
        ]
        ids = {}
        for title, text in texts:
            status, res = client.post("/api/documents", {"title": title, "text": text})
            eq(status, 201)
            ids[title] = res["document"]["id"]
        status, res = client.post("/api/tasks", {"threshold": 0.5, "phrase_n": 3,
                                                 "mode": "exact"})
        eq(status, 201)
        task_id = res["task"]["id"]
        task = _wait_task(client, task_id)
        eq(task["status"], "completed")
        eq(task["mode"], "exact")
        ok(task["edge_count"] >= 1)
        status, edges = client.get("/api/tasks/%s/edges" % task_id)
        eq(status, 200)
        eq(edges["total"], task["edge_count"])
        pair = next(e for e in edges["edges"]
                    if {e["doc_a"], e["doc_b"]} == {ids["one"], ids["two"]})
        status, detail = client.get("/api/edges/%d" % pair["edge_id"])
        eq(status, 200)
        ok(detail["alignment"]["available"])
        ok(len(detail["alignment"]["segments"]) >= 1)
        eq(detail["independent_check"]["above_threshold"], True)
        # verify endpoint
        status, report = client.post("/api/tasks/%s/verify" % task_id)
        eq(status, 200)
        eq(report["verdict"], "pass")
        eq(report["missing_total"], 0)
    finally:
        server.shutdown(); cleanup(tmp)


def _seed_chain(client):
    ids = {}
    for title, text in [("A", CHAIN_A), ("B", CHAIN_B), ("C", CHAIN_C)]:
        status, res = client.post("/api/documents", {"title": title, "text": text})
        eq(status, 201); ids[title] = res["document"]["id"]
    status, res = client.post("/api/tasks", {"threshold": 0.55, "mode": "exact"})
    task_id = res["task"]["id"]
    task = _wait_task(client, task_id)
    eq(task["status"], "completed")
    return ids, task_id, task


def test_decisions_and_group_rebuild_over_http():
    tmp, server, thread, client = _server()
    try:
        ids, task_id, task = _seed_chain(client)
        status, groups = client.get("/api/tasks/%s/groups" % task_id)
        eq(status, 200)
        eq(len(groups["groups"]), 1)
        group = groups["groups"][0]
        eq(len(group["members"]), 3)
        eq(len(group["pairs"]), 3, "every member pair is listed")
        holes = [p for p in group["pairs"] if p["edge"] is None]
        eq(len(holes), 1, "the non-transitive A/C pair is explicitly listed as a hole")
        hole = holes[0]
        eq({hole["doc_a"], hole["doc_b"]}, {ids["A"], ids["C"]})
        eq(group["edges"][0]["provenance"], "candidate")
        status, edges = client.get("/api/tasks/%s/edges" % task_id)
        eq(edges["total"], 2)
        # marking B-C keep-separate splits the component at HTTP level
        bc = next(e for e in edges["edges"]
                  if {e["doc_a"], e["doc_b"]} == {ids["B"], ids["C"]})
        status, res = client.post("/api/edges/%d/decision" % bc["edge_id"],
                                  {"decision": "keep_separate"})
        eq(status, 200)
        ok(res["group_rebuild"]["splits"], "splitting is reported")
        status, groups2 = client.get("/api/tasks/%s/groups" % task_id)
        members = [sorted(m["id"] for m in g["members"]) for g in groups2["groups"]]
        eq(members, [sorted([ids["A"], ids["B"]])])
        status, err = client.post("/api/edges/999999/decision", {"decision": "same_group"})
        eq(status, 404)
        status, err = client.post("/api/tasks/%s/run" % task_id, {"threshold": 0.9})
        eq(status, 400, "threshold cannot change after task creation")
    finally:
        server.shutdown(); cleanup(tmp)


def test_group_split_via_withdraw_http():
    tmp, server, thread, client = _server()
    try:
        ids, task_id, task = _seed_chain(client)
        status, groups = client.get("/api/tasks/%s/groups" % task_id)
        eq(len(groups["groups"][0]["members"]), 3)
        status, res = client.post("/api/documents/%s/withdraw" % ids["B"],
                                  {"reason": "duplicate of nothing"})
        eq(status, 200)
        rebuild = res["group_rebuild"]
        eq(rebuild["groups"], 0, "A and C were only connected through B")
        ok(rebuild["splits"], "the split is reported")
        eq(rebuild["splits"][0]["withdrawn_members"], [ids["B"]])
        status, groups2 = client.get("/api/tasks/%s/groups" % task_id)
        eq(groups2["groups"], [])
        status, res = client.post("/api/documents/%s/restore" % ids["B"])
        eq(status, 200)
        eq(res["group_rebuild"]["groups"], 1, "restoring rejoins the group")
        status, doc = client.get("/api/documents/%s" % ids["B"])
        eq(doc["status"], "active")
    finally:
        server.shutdown(); cleanup(tmp)


def test_export_endpoints():
    tmp, server, thread, client = _server()
    try:
        status, res = client.post("/api/documents", {"title": "A", "text": LONG})
        doc_id = res["document"]["id"]
        status, res = client.post("/api/documents",
                                  {"title": "B", "text": LONG + " Extra line here."})
        status, res = client.post("/api/tasks", {"threshold": 0.5, "mode": "exact"})
        task_id = res["task"]["id"]
        _wait_task(client, task_id)
        status, manifest = client.get("/api/tasks/%s/export?format=manifest" % task_id)
        eq(status, 200)
        eq(manifest["export_version"], "corpuslake.manifest.v1")
        ok(manifest["similarity_semantics"]["not_transitive"])
        status, csv_bytes = client.get("/api/tasks/%s/export?format=edges.csv" % task_id)
        eq(status, 200)
        contains(csv_bytes.decode("utf-8"), "jaccard")
        status, bundle = client.get("/api/tasks/%s/export?format=bundle" % task_id)
        eq(status, 200)
        ok(isinstance(bundle, bytes) and bundle[:2] == b"PK")
    finally:
        server.shutdown(); cleanup(tmp)


def test_approx_mode_is_labelled():
    tmp, server, thread, client = _server()
    try:
        for i in range(6):
            client.post("/api/documents", {"title": "d%d" % i, "text": LONG + " variant %d." % i})
        status, res = client.post("/api/tasks", {"threshold": 0.5, "mode": "approx"})
        task_id = res["task"]["id"]
        task = _wait_task(client, task_id)
        eq(task["mode"], "approx")
        contains(task["completeness"], "APPROXIMATE")
        status, report = client.post("/api/tasks/%s/verify" % task_id)
        eq(status, 200)
        ok(report["verdict"] in ("pass", "approx-incomplete"))
        contains(report["completeness_claim"], "approx")
    finally:
        server.shutdown(); cleanup(tmp)


def test_stop_then_resume_over_http():
    """Stopping and immediately resuming must never run two workers at once."""
    tmp, server, thread, client = _server()
    try:
        words = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
                 "nu xi omicron pi rho sigma tau upsilon phi chi psi omega").split()
        for i in range(120):
            text = " ".join("%s%d" % (words[(i + j) % len(words)], (i * 7 + j) % 300)
                            for j in range(160))
            status, _res = client.post("/api/documents", {"title": "d%d" % i, "text": text})
            eq(status, 201)
        status, res = client.post("/api/tasks", {"threshold": 0.9, "mode": "exact"})
        eq(status, 201)
        task_id = res["task"]["id"]
        status, stop = client.post("/api/tasks/%s/stop" % task_id)
        eq(status, 200)
        # Resume right away: either it waits for the stopped worker or says so.
        status, resumed = client.post("/api/tasks/%s/run" % task_id)
        eq(status, 200)
        if not resumed.get("started"):
            eq(resumed.get("reason"), "stopping")
            time.sleep(0.2)
            status, resumed = client.post("/api/tasks/%s/run" % task_id)
            eq(status, 200)
            eq(resumed.get("started"), True)
        task = _wait_task(client, task_id, timeout=60.0)
        eq(task["status"], "completed")
        status, progress = client.get("/api/tasks/%s" % task_id)
        eq(progress["status"], "completed")
        eq(progress["progress"]["phase"], "completed",
           "the resumed run replaces the stopped run's progress")
        report = client.post("/api/tasks/%s/verify" % task_id)[1]
        eq(report["verdict"], "pass")
        eq(report["documents"], 120)
    finally:
        server.shutdown(); cleanup(tmp)


CASES = [
    ("api: health and rules", test_health_and_rules),
    ("api: identical uploads need an explicit decision", test_upload_identical_conflict_and_duplicate_identity),
    ("api: empty document accepted, second needs identity decision", test_empty_document_accepted),
    ("api: task lifecycle, edges, compare detail, verify", test_task_lifecycle_and_polling),
    ("api: groups list every pair, never a clique", test_decisions_and_group_rebuild_over_http),
    ("api: withdrawal splits and restore rejoins", test_group_split_via_withdraw_http),
    ("api: export manifest / csv / bundle", test_export_endpoints),
    ("api: approximate mode labelled everywhere", test_approx_mode_is_labelled),
    ("api: stop then resume never overlaps two workers", test_stop_then_resume_over_http),
]

if __name__ == "__main__":
    raise SystemExit(run_cases(CASES))
