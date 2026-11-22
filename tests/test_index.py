"""Integration tests for the index pipeline, grouping, withdrawal, resume, exports.

Run: python3 -m tests.test_index
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from tests.harness import approx, contains, eq, make_tmp, cleanup, ok, run_cases
from corpuslake import (exports, grouping, indexing, ingest, textrules,
                        verification)
from corpuslake.store import Store


CHAIN = {
    "A": ("Coastal wetland surveys combine hydrology sediment transport and riparian "
          "ecology into a single long term monitoring plan. Field crews measure water "
          "quality at fixed stations every season and compare the results with baseline "
          "surveys collected before the dam removal. Public reports summarise the "
          "findings and recommend adaptive management for the following decade."),
    "B": ("River restoration projects combine hydrology sediment transport and riparian "
          "ecology into a single long term monitoring plan. Field crews measure water "
          "quality at fixed stations every season and compare the results with baseline "
          "surveys collected before the dam removal. Public reports summarise the "
          "findings and recommend adaptive management for the following decade."),
    "C": ("River restoration projects combine hydrology sediment transport and riparian "
          "ecology into a single long term monitoring plan. Field crews measure water "
          "quality at fixed stations every season and compare the results with baseline "
          "surveys collected before the dam removal. Newspaper editorials attack the "
          "budget and demand that legislators cancel the remaining funding immediately."),
    "EXACT": ("The city council approved the new transit corridor after a long public "
              "consultation. Construction begins next spring and will last thirty months. "
              "The project adds twelve kilometres of dedicated bus lanes and four new "
              "stations with step free access."),
    "NOISY": ("The city council approved the new transit corridor after a long public\n"
              "consultation!  Construction begins next spring and will last thirty months.\n"
              "The project adds twelve kilometres of dedicated bus lanes and four new\n"
              "stations with step-free access."),
    "REPEAT": ("Archive policy requires three copies of every record. Archive policy "
               "requires three copies of every record. Archive policy requires three "
               "copies of every record. Retention schedules are reviewed each year."),
    "SHORT": "Too short.",
    "EMPTY": "",
    "CN_A": ("城市轨道交通新线路在长期公众咨询后获得批准。工程将于明年春季开工，"
             "持续三十个月，新增十二公里公交专用道和四座无障碍车站。"),
    "CN_B": ("城市轨道交通新线路在长期公众咨询后获得批准。工程将于明年春季开工，"
             "持续三十个月，新增十二公里公交专用道和四座无障碍车站！"),
    "OTHER": ("A recipe for rye sourdough: refresh the starter twelve hours before mixing, "
              "keep the dough cool, and bake in a covered pot for the first twenty minutes."),
}

THRESHOLD = 0.55


def _seed(threshold=THRESHOLD, phrase_n=3, mode="exact", keys=None):
    tmp = make_tmp()
    store = Store(tmp / "test.db")
    ids = {}
    for key in (keys or CHAIN):
        row, _created = store.add_document(key, CHAIN[key])
        ids[key] = row["id"]
    options = indexing.BuildOptions(threshold=threshold, phrase_n=phrase_n, mode=mode)
    fp = store.ensure_ruleset(options.tokenizer_version, options.phrase_version,
                              options.phrase_n, options.vocab_version)
    task = store.create_task(fp, mode, threshold)
    progress = indexing.IndexWorker(store, task["id"], options).run()
    grouping.rebuild_groups(store, task["id"])
    return tmp, store, task["id"], ids, progress


def _edges(store, task_id):
    return {(e["doc_a"], e["doc_b"]): e for e in grouping.all_edges(store, task_id)}


def _pair(ids, a, b):
    x, y = sorted((ids[a], ids[b]))
    return (x, y)


def test_index_completes_and_verifies():
    tmp, store, task_id, ids, progress = _seed()
    try:
        eq(progress.phase, "completed")
        task = store.get_task(task_id)
        eq(task["status"], "completed")
        report = verification.verify_task(store, task_id)
        eq(report["verdict"], "pass", "exact index must agree with full pairwise truth")
        eq(report["missing_total"], 0)
        eq(report["extra_total"], 0)
        eq(report["score_mismatch_total"], 0)
        eq(report["documents"], len(CHAIN))
        eq(report["pairs_compared"], len(CHAIN) * (len(CHAIN) - 1) // 2)
    finally:
        store.close(); cleanup(tmp)


def test_chain_is_not_transitive_and_group_keeps_edges():
    """A~B and B~C but A!~C at this threshold: one component, three pairs, one hole."""
    tmp, store, task_id, ids, progress = _seed()
    try:
        edges = _edges(store, task_id)
        ab = edges.get(_pair(ids, "A", "B"))
        bc = edges.get(_pair(ids, "B", "C"))
        ac = edges.get(_pair(ids, "A", "C"))
        ok(ab is not None, "A-B should be an edge")
        ok(bc is not None, "B-C should be an edge")
        ok(ac is None, "A-C must NOT reach the threshold")
        eq(ab["provenance"], "candidate", "exact mode provenance")
        groups = grouping.group_detail_rows(store, task_id)
        chain_group = [g for g in groups
                       if {ids["A"], ids["B"], ids["C"]} <= {m["id"] for m in g["members"]}]
        eq(len(chain_group), 1, "A, B, C are one connected component")
        pairs = {tuple(sorted((p["doc_a"], p["doc_b"]))) for p in chain_group[0]["pairs"]}
        ok(_pair(ids, "A", "C") in pairs, "the missing A-C pair is listed explicitly")
        hole = [p for p in chain_group[0]["pairs"] if tuple(sorted((p["doc_a"], p["doc_b"]))) == _pair(ids, "A", "C")]
        eq(hole[0]["edge"], None, "A-C pair carries edge=null, never an implied clique")
        eq(len(chain_group[0]["edges"]), 2)
    finally:
        store.close(); cleanup(tmp)


def test_empty_short_and_identical_documents():
    tmp, store, task_id, ids, progress = _seed()
    try:
        empty = store.get_document(ids["EMPTY"])
        eq(bool(empty["is_empty"]), True)
        eq(empty["token_count"], 0)
        edges = _edges(store, task_id)
        for pair, edge in edges.items():
            ok(ids["EMPTY"] not in pair, "empty document never gets a similarity edge")
        short = store.get_document(ids["SHORT"])
        ok(short["token_count"] > 0 and short["phrase_count"] == 0)
        for pair in edges:
            ok(ids["SHORT"] not in pair, "documents shorter than n are not matched")
        # identical uploads become separate identities and an identical-hash edge
        row2, _ = store.add_document("EXACT duplicate", CHAIN["EXACT"])
        ok(row2["id"] != ids["EXACT"], "identical text is a separate identity")
        eq(store.find_identical(CHAIN["EXACT"])["id"], ids["EXACT"],
           "find_identical points at the first stored copy")
        # rebuild to include it
        task = store.get_task(task_id)
        options = indexing.BuildOptions(threshold=THRESHOLD, phrase_n=3, mode="exact")
        indexing.IndexWorker(store, task_id, options).run()
        grouping.rebuild_groups(store, task_id)
        edges = _edges(store, task_id)
        pair = tuple(sorted((ids["EXACT"], row2["id"])))
        ok(pair in edges, "byte-identical texts produce an edge")
        eq(edges[pair]["jaccard"], 1.0)
        eq(edges[pair]["candidate_reason"], "identical-hash")
    finally:
        store.close(); cleanup(tmp)


def test_noisy_near_duplicate_and_cjk():
    tmp, store, task_id, ids, progress = _seed()
    try:
        edges = _edges(store, task_id)
        pair = _pair(ids, "EXACT", "NOISY")
        ok(pair in edges, "formatting-only differences stay similar")
        ok(edges[pair]["jaccard"] >= 0.85, "noise must not destroy similarity")
        cn = _pair(ids, "CN_A", "CN_B")
        ok(cn in edges, "CJK text is compared as character n-grams")
        eq(edges[cn]["jaccard"], 1.0)
    finally:
        store.close(); cleanup(tmp)


def test_repeated_phrases_do_not_inflate_score():
    tmp, store, task_id, ids, progress = _seed()
    try:
        rev = store.revision_for(ids["REPEAT"], store.get_task(task_id)["rules_fp"])
        phrases = json.loads(rev["phrases_json"])
        counts = [c for _k, c in phrases]
        ok(max(counts) > 1, "repetition is recorded in the occurrence count")
        ok(len({k for k, _c in phrases}) == len(phrases),
           "phrase rows are deduplicated")
        # repeat vs other scores by *set* overlap, not by raw occurrence volume
        check = verification.verify_pair(store, task_id, ids["REPEAT"], ids["OTHER"])
        eq(check["union_size"], check["phrase_count_a"] + check["phrase_count_b"]
           - check["intersection"])
    finally:
        store.close(); cleanup(tmp)


def test_withdraw_splits_group_then_restore_rejoins():
    tmp, store, task_id, ids, progress = _seed()
    try:
        groups = grouping.group_detail_rows(store, task_id)
        size_before = next(len(g["members"]) for g in groups
                           if ids["B"] in {m["id"] for m in g["members"]})
        ok(size_before >= 3)
        store.withdraw_document(ids["B"], "test withdrawal")
        report = grouping.rebuild_groups(store, task_id)
        ok(report.splits, "removing the bridge document must split the component")
        split = report.splits[0]
        eq(split["old_group"] is not None, True)
        eq(sorted(split["members"]), sorted([ids["A"], ids["B"], ids["C"]]))
        eq(split["withdrawn_members"], [ids["B"]])
        eq(len(split["new_groups"]), 2, "the component broke into two singleton branches")
        groups_after = grouping.group_detail_rows(store, task_id)
        chain_members = {ids["A"], ids["B"], ids["C"]}
        still_chain = [g for g in groups_after
                       if chain_members & {m["id"] for m in g["members"]}]
        eq(still_chain, [], "A and C are no longer connected to anything in the chain")
        ok(ids["A"] in report.independent and ids["C"] in report.independent)
        ok(ids["B"] not in report.independent, "withdrawn docs are not listed as active")
        eq(store.get_document(ids["B"])["text"], CHAIN["B"],
           "withdrawal keeps the original text")
        store.restore_document(ids["B"])
        report2 = grouping.rebuild_groups(store, task_id)
        groups_restored = grouping.group_detail_rows(store, task_id)
        ok(any({ids["A"], ids["B"], ids["C"]} <= {m["id"] for m in g["members"]}
               for g in groups_restored), "restoring rejoins the chain")
        ok(report2.run_id != report.run_id)
    finally:
        store.close(); cleanup(tmp)


def test_edge_decision_removes_edge_and_splits():
    tmp, store, task_id, ids, progress = _seed()
    try:
        edges = _edges(store, task_id)
        bc = edges[_pair(ids, "B", "C")]
        grouping.set_edge_decision(store, bc["id"], "keep_separate", "review says no")
        report = grouping.rebuild_groups(store, task_id)
        ok(report.splits, "removing B-C splits the connected component")
        groups = grouping.group_detail_rows(store, task_id)
        pair_group = [g for g in groups
                      if {m["id"] for m in g["members"]} == {ids["A"], ids["B"]}]
        eq(len(pair_group), 1, "A-B remain connected")
        c_groups = [g for g in groups if ids["C"] in {m["id"] for m in g["members"]}]
        eq(c_groups, [], "C is now independent")
        # the edge row itself is kept as evidence, with the decision attached
        kept = grouping.all_edges(store, task_id)
        row = next(e for e in kept if e["id"] == bc["id"])
        eq(row["decision"], "keep_separate")
        # decision is keyed by (task, pair): a fresh task is unreviewed
        task = store.get_task(task_id)
        options = indexing.BuildOptions(threshold=THRESHOLD, phrase_n=3, mode="exact")
        fp = store.ensure_ruleset(options.tokenizer_version, options.phrase_version,
                                  options.phrase_n, options.vocab_version)
        task2 = store.create_task(fp, "exact", THRESHOLD)
        indexing.IndexWorker(store, task2["id"], options).run()
        edges2 = {(e["doc_a"], e["doc_b"]): e for e in grouping.all_edges(store, task2["id"])}
        eq(edges2[_pair(ids, "B", "C")]["decision"], "unreviewed",
           "review decisions do not leak into a new task")
    finally:
        store.close(); cleanup(tmp)


def test_resume_after_stop_keeps_progress():
    tmp = make_tmp()
    store = Store(tmp / "resume.db")
    try:
        ids = {}
        for key in CHAIN:
            ids[key] = store.add_document(key, CHAIN[key])[0]["id"]
        options = indexing.BuildOptions(threshold=THRESHOLD, phrase_n=3, mode="exact")
        fp = store.ensure_ruleset(options.tokenizer_version, options.phrase_version,
                                  options.phrase_n, options.vocab_version)
        task = store.create_task(fp, "exact", THRESHOLD)
        state = {"count": 0}

        def stop_after_three():
            state["count"] += 1
            return state["count"] > 3

        worker = indexing.IndexWorker(store, task["id"], options,
                                      stop=stop_after_three)
        first = worker.run()
        eq(first.phase, "stopped")
        eq(store.get_task(task["id"])["status"], "stopped")
        done_first = store.get_task(task["id"])["done"]
        ok(0 < done_first < len(CHAIN), "some documents were committed before stopping")
        revs_first = store.db.execute(
            "SELECT COUNT(*) AS n FROM doc_revisions WHERE rules_fp=?", (fp,)
        ).fetchone()["n"]
        eq(revs_first, done_first)
        # resume: worker sees existing revisions and continues
        second = indexing.IndexWorker(store, task["id"], options).run()
        eq(second.phase, "completed")
        revs_total = store.db.execute(
            "SELECT COUNT(*) AS n FROM doc_revisions WHERE rules_fp=?", (fp,)
        ).fetchone()["n"]
        eq(revs_total, len(CHAIN), "every document has a revision after resume")
        report = verification.verify_task(store, task["id"])
        eq(report["verdict"], "pass")
    finally:
        store.close(); cleanup(tmp)


def test_rule_change_invalidates_old_index():
    tmp, store, task_id, ids, progress = _seed(phrase_n=3)
    try:
        task = store.get_task(task_id)
        old_fp = task["rules_fp"]
        options = indexing.BuildOptions(threshold=THRESHOLD, phrase_n=4, mode="exact")
        new_fp = store.ensure_ruleset(options.tokenizer_version, options.phrase_version,
                                      options.phrase_n, options.vocab_version)
        ok(new_fp != old_fp, "n=4 has a different rule-set fingerprint")
        task2 = store.create_task(new_fp, "exact", THRESHOLD)
        indexing.IndexWorker(store, task2["id"], options).run()
        revs_old = store.db.execute(
            "SELECT COUNT(*) AS n FROM doc_revisions WHERE rules_fp=?", (old_fp,)
        ).fetchone()["n"]
        revs_new = store.db.execute(
            "SELECT COUNT(*) AS n FROM doc_revisions WHERE rules_fp=?", (new_fp,)
        ).fetchone()["n"]
        ok(revs_old > 0 and revs_new > 0, "both rule sets have their own revisions")
        # a task pinned to the old fingerprint refuses to run with new options
        try:
            indexing.IndexWorker(store, task2["id"], indexing.BuildOptions(
                threshold=THRESHOLD, phrase_n=3, mode="exact")).run()
        except ValueError as exc:
            contains(str(exc), "rule set")
        else:
            raise AssertionError("worker must reject a mismatched rule set")
        # vocabulary digests are per rule set / snapshot
        eq(store.get_task(task_id)["vocab_digest"] is not None, True)
        # old edges still exist for the old task and are not reused by the new one
        old_edges = grouping.all_edges(store, task_id)
        new_edges = grouping.all_edges(store, task2["id"])
        ok(old_edges and new_edges)
        ok(all(e["rules_fp"] == old_fp for e in old_edges))
        ok(all(e["rules_fp"] == new_fp for e in new_edges))
    finally:
        store.close(); cleanup(tmp)


def test_snapshot_staleness_is_visible():
    tmp, store, task_id, ids, progress = _seed()
    try:
        task = store.get_task(task_id)
        snapshot = json.loads(task["snapshot_json"])
        eq(set(snapshot), set(ids.values()))
        new_row, _ = store.add_document("Late arrival", CHAIN["OTHER"] + " Extra.")
        snapshot_now = json.loads(store.get_task(task_id)["snapshot_json"])
        ok(new_row["id"] not in snapshot_now,
           "a later upload is not silently part of the finished task's snapshot")
        # verification reports missing revisions rather than pretending
        report = verification.verify_task(store, task_id)
        ok(new_row["id"] in report["missing_revisions"],
           "verification names the document outside the snapshot")
    finally:
        store.close(); cleanup(tmp)


def test_exports_do_not_claim_cliques():
    tmp, store, task_id, ids, progress = _seed()
    try:
        manifest = exports.corpus_manifest(store, task_id)
        ok(manifest["similarity_semantics"]["not_transitive"] is True)
        eq(manifest["similarity_semantics"]["group_kind"],
           "connected component of accepted edges")
        eq(len(manifest["documents"]), len(CHAIN))
        # full original text is exported
        eq(manifest["documents"][0]["text"] is not None, True)
        group = next(g for g in manifest["groups"]
                     if {ids["A"], ids["B"], ids["C"]} <= {m["id"] for m in g["members"]})
        holes = [p for p in group["pairs"] if p["edge"] is None]
        eq(len(holes), 1, "the A-C hole is exported explicitly")
        csv_text = exports.edges_csv(store, task_id)
        header = csv_text.splitlines()[0]
        contains(header, "jaccard")
        eq(len(csv_text.strip().splitlines()) - 1, len(grouping.all_edges(store, task_id)))
        nq = exports.links_nquads(store, task_id)
        ok(nq.count("urn:corpuslake:jaccard") == len(grouping.all_edges(store, task_id)))
        dest = tmp / "exports"
        path = exports.export_bundle(store, dest, task_id)
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            eq(names, {"manifest.json", "edges.json", "edges.csv", "groups.json",
                       "groups_flat.json", "links.nq", "documents.csv", "rules.json"})
            bundle_manifest = json.loads(zf.read("manifest.json"))
            eq(bundle_manifest["export_version"], "corpuslake.manifest.v1")
            ok(b'"not_transitive": true' in zf.read("manifest.json"))
    finally:
        store.close(); cleanup(tmp)


def test_ingest_formats(tmp):
    pass


def test_ingest_jsonl_csv_and_directory():
    tmp = make_tmp()
    corpus = tmp / "corpus"
    corpus.mkdir()
    (corpus / "one.md").write_text("# Note\n\nAlpha beta gamma delta epsilon zeta.", encoding="utf-8")
    (corpus / "two.txt").write_text("Alpha beta gamma delta epsilon zeta!", encoding="utf-8")
    (corpus / "items.jsonl").write_text(
        json.dumps({"title": "J1", "text": "alpha beta gamma delta epsilon zeta"},
                   ensure_ascii=False) + "\n" +
        json.dumps({"id": "J2", "text": "completely unrelated words here now"},
                   ensure_ascii=False) + "\n", encoding="utf-8")
    (corpus / "rows.csv").write_text(
        "title,text\nC1,alpha beta gamma delta epsilon zeta\n", encoding="utf-8")
    (corpus / "list.json").write_text(json.dumps(
        {"documents": [{"title": "X1", "text": "alpha beta gamma delta epsilon zeta"}]}),
        encoding="utf-8")
    (corpus / "bad.json").write_text("{not json", encoding="utf-8")
    store = Store(tmp / "ingest.db")
    try:
        result = ingest.import_directory(store, corpus)
        eq(result["imported"], 6)
        eq(len(result["errors"]), 1, "malformed JSON is reported, not fatal")
        titles = {d["title"] for d in store.list_documents()}
        for expected in ("one", "two", "J1", "J2", "C1", "X1"):
            ok(expected in titles, "imported %s" % expected)
    finally:
        store.close(); cleanup(tmp)


def test_verification_samples_large_corpora_deterministically():
    tmp = make_tmp()
    store = Store(tmp / "large.db")
    try:
        words = ("alpha beta gamma delta epsilon zeta eta theta iota kappa").split()
        for i in range(60):
            text = " ".join("%s%d" % (words[(i + j) % 10], (i * 3 + j) % 200)
                            for j in range(80))
            store.add_document("d%02d" % i, text)
        options = indexing.BuildOptions(threshold=0.75, phrase_n=3, mode="exact")
        fp = store.ensure_ruleset(options.tokenizer_version, options.phrase_version,
                                  options.phrase_n, options.vocab_version)
        task = store.create_task(fp, "exact", 0.75)
        indexing.IndexWorker(store, task["id"], options).run()
        first = verification.verify_task(store, task["id"], sample_size=20)
        eq(first["scope"], "sample")
        eq(first["documents"], 20)
        eq(first["documents_total"], 60)
        contains(first["completeness_claim"], "sampled")
        second = verification.verify_task(store, task["id"], sample_size=20)
        eq([m["doc_a"] for m in first["missing"]], [m["doc_a"] for m in second["missing"]],
           "the sample is deterministic for a task")
        eq(second["pairs_compared"], 20 * 19 // 2)
        eq(first["verdict"], "pass", "sample scope still catches real false negatives")
        full = verification.verify_task(store, task["id"], full=True)
        eq(full["scope"], "full")
        eq(full["documents"], 60)
        eq(full["pairs_compared"], 60 * 59 // 2)
        eq(full["verdict"], "pass")
        # a low sample cap still works and is labelled, not an error
        small = verification.verify_task(store, task["id"], sample_size=5)
        eq(small["documents"], 5)
        eq(small["pairs_compared"], 10)
    finally:
        store.close(); cleanup(tmp)


CASES = [
    ("index: completes and full-pairwise verification passes", test_index_completes_and_verifies),
    ("index: non-transitive chain, group keeps edges and the hole", test_chain_is_not_transitive_and_group_keeps_edges),
    ("index: empty / short / identical documents", test_empty_short_and_identical_documents),
    ("index: noisy duplicate and CJK text", test_noisy_near_duplicate_and_cjk),
    ("index: repeated phrases collapse, no score inflation", test_repeated_phrases_do_not_inflate_score),
    ("groups: withdraw splits, restore rejoins", test_withdraw_splits_group_then_restore_rejoins),
    ("groups: keep-separate edge splits, decisions are per task", test_edge_decision_removes_edge_and_splits),
    ("task: stop then resume keeps committed progress", test_resume_after_stop_keeps_progress),
    ("rules: changing n creates a new index; old index not reused", test_rule_change_invalidates_old_index),
    ("task: new uploads are visibly outside the finished snapshot", test_snapshot_staleness_is_visible),
    ("export: manifest/edges/groups never imply cliques", test_exports_do_not_claim_cliques),
    ("verify: large corpora use a deterministic labelled sample", test_verification_samples_large_corpora_deterministically),
    ("ingest: md/txt/jsonl/csv/json + error tolerance", test_ingest_jsonl_csv_and_directory),
]

if __name__ == "__main__":
    raise SystemExit(run_cases(CASES))
