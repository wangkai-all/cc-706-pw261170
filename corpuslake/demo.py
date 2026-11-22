"""A deterministic demo corpus that exercises every edge case the tool claims.

The seeded documents deliberately include:

* a chain A~B~C where A and C are *not* similar (non-transitivity);
* byte-identical duplicates;
* a near-duplicate with punctuation/whitespace noise;
* Chinese text with no spaces;
* an empty document;
* a document shorter than one phrase;
* repeated phrases inside one document (duplicate collapse);
* a document that the demo withdrawal step removes from the middle of a group.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import indexing, grouping, textrules
from .store import Store

CHAIN_B = (
    "River restoration projects combine hydrology sediment transport and riparian "
    "ecology into a single long term monitoring plan. Field crews measure water "
    "quality at fixed stations every season and compare the results with baseline "
    "surveys collected before the dam removal. Public reports summarise the "
    "findings and recommend adaptive management for the following decade."
)

CHAIN_A = (
    "Coastal wetland surveys combine hydrology sediment transport and riparian "
    "ecology into a single long term monitoring plan. Field crews measure water "
    "quality at fixed stations every season and compare the results with baseline "
    "surveys collected before the dam removal. Public reports summarise the "
    "findings and recommend adaptive management for the following decade."
)

CHAIN_C = (
    "River restoration projects combine hydrology sediment transport and riparian "
    "ecology into a single long term monitoring plan. Field crews measure water "
    "quality at fixed stations every season and compare the results with baseline "
    "surveys collected before the dam removal. Newspaper editorials attack the "
    "budget and demand that legislators cancel the remaining funding immediately."
)

EXACT = (
    "The city council approved the new transit corridor after a long public "
    "consultation. Construction begins next spring and will last thirty months. "
    "The project adds twelve kilometres of dedicated bus lanes and four new "
    "stations with step free access."
)

NOISY = (
    "The city council approved the new transit corridor after a long public\n"
    "consultation!  Construction begins next spring and will last thirty months.\n"
    "The project adds twelve kilometres of dedicated bus lanes and four new\n"
    "stations with step-free access."
)

CHINESE_A = (
    "城市轨道交通新线路在长期公众咨询后获得批准。工程将于明年春季开工，"
    "持续三十个月，新增十二公里公交专用道和四座无障碍车站。"
)

CHINESE_B = (
    "城市轨道交通新线路在长期公众咨询后获得批准。工程将于明年春季开工，"
    "持续三十个月，新增十二公里公交专用道和四座无障碍车站！"
)

REPEAT = (
    "Archive policy requires three copies of every record. Archive policy requires "
    "three copies of every record. Archive policy requires three copies of every "
    "record. Retention schedules are reviewed each year."
)

SHORT = "Too short."

OTHER = (
    "A recipe for rye sourdough: refresh the starter twelve hours before mixing, "
    "keep the dough cool, and bake in a covered pot for the first twenty minutes."
)


DEMO_DOCS = [
    ("River restoration monitoring plan (base)", CHAIN_B, "demo"),
    ("Coastal wetland survey memo", CHAIN_A, "demo"),
    ("River restoration editorial response", CHAIN_C, "demo"),
    ("Transit corridor approval", EXACT, "demo"),
    ("Transit corridor approval (formatted)", NOISY, "demo"),
    ("Transit corridor approval (second upload)", EXACT, "demo"),
    ("城市轨道新线（甲）", CHINESE_A, "demo"),
    ("城市轨道新线（乙）", CHINESE_B, "demo"),
    ("Archive retention note", REPEAT, "demo"),
    ("Too short", SHORT, "demo"),
    ("Empty upload", "", "demo"),
    ("Rye sourdough recipe", OTHER, "demo"),
]


def seed(store: Store, reset: bool = False, db_path: str = "") -> dict:
    existing = store.list_documents()
    if existing and not reset:
        return {"seeded": False, "documents": len(existing),
                "message": "database already has documents; pass --reset to reseed"}
    if reset:
        for row in existing:
            store.db.execute("DELETE FROM documents WHERE id=?", (row["id"],))
        store.db.commit()

    created = []
    for title, text, source in DEMO_DOCS:
        row, made = store.add_document(
            title, text, source=source,
            duplicate_of=None if not text else None)
        created.append({"title": title, "doc_id": row["id"], "created": made,
                        "is_empty": bool(row["is_empty"])})

    options = indexing.BuildOptions(threshold=0.55, phrase_n=3, mode="exact")
    fp = store.ensure_ruleset(options.tokenizer_version, options.phrase_version,
                              options.phrase_n, options.vocab_version)
    task = store.create_task(fp, "exact", options.threshold, note="demo task")
    progress = indexing.IndexWorker(store, task["id"], options).run()
    report = grouping.rebuild_groups(store, task["id"])
    return {
        "seeded": True,
        "documents": len(created),
        "task_id": task["id"],
        "progress": progress.as_dict(),
        "groups": report.as_dict(),
        "created": created,
    }
