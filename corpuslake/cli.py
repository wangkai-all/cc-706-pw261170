"""Command-line interface: serve the UI and run pipeline steps headlessly."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import exports, grouping, indexing, ingest, textrules, verification
from .store import Store, now


DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "corpuslake.db"


def _store(args) -> Store:
    return Store(Path(args.db))


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_init(args) -> int:
    store = _store(args)
    store.close()
    _print({"ok": True, "db": str(args.db)})
    return 0


def cmd_import(args) -> int:
    store = _store(args)
    try:
        path = Path(args.path).expanduser()
        if path.is_dir():
            result = ingest.import_directory(store, path)
        else:
            report = ingest.import_file(store, path)
            result = {"imported": report.imported, "skipped": report.skipped,
                      "errors": report.errors}
        _print(result)
        return 0 if not result["errors"] or result["imported"] else 1
    finally:
        store.close()


def cmd_index(args) -> int:
    store = _store(args)
    try:
        options = indexing.BuildOptions(
            threshold=args.threshold, phrase_n=args.phrase_n, mode=args.mode,
            num_perm=args.num_perm)
        options.validate()
        fp = store.ensure_ruleset(options.tokenizer_version, options.phrase_version,
                                  options.phrase_n, options.vocab_version)
        task = store.create_task(fp, options.mode, options.threshold,
                                 note="created from CLI")
        stop = {"flag": False}
        worker = indexing.IndexWorker(
            store, task["id"], options, stop=lambda: stop["flag"],
            progress=lambda p: print("[%s] %d/%d %s" % (p.phase, p.done, p.total,
                                                        p.message),
                                     file=sys.stderr))
        result = worker.run()
        report = grouping.rebuild_groups(store, task["id"]).as_dict()
        _print({"task_id": task["id"], "progress": result.as_dict(),
                "groups": report, "rules_fp": fp,
                "vocab_digest": store.get_task(task["id"])["vocab_digest"]})
        return 0 if result.phase == "completed" else 2
    finally:
        store.close()


def cmd_verify(args) -> int:
    store = _store(args)
    try:
        task_id = args.task or (store.latest_task() or {})["id"]
        result = verification.verify_task(store, task_id, sample_size=args.sample,
                                          full=args.full)
        _print(result)
        return 0 if result["verdict"] in ("pass", "approx-incomplete") else 3
    finally:
        store.close()


def cmd_export(args) -> int:
    store = _store(args)
    try:
        task_id = args.task or (store.latest_task() or {})["id"]
        if args.format == "bundle":
            path = exports.export_bundle(store, Path(args.out).parent, task_id)
            print(path)
        elif args.format == "manifest":
            out = exports.corpus_manifest(store, task_id)
            Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
            print(args.out)
        elif args.format == "edges.csv":
            Path(args.out).write_text(exports.edges_csv(store, task_id), encoding="utf-8")
            print(args.out)
        else:
            Path(args.out).write_text(exports.links_nquads(store, task_id), encoding="utf-8")
            print(args.out)
        return 0
    finally:
        store.close()


def cmd_groups(args) -> int:
    store = _store(args)
    try:
        task_id = args.task or (store.latest_task() or {})["id"]
        report = grouping.rebuild_groups(store, task_id, args.basis)
        _print(report.as_dict())
        return 0
    finally:
        store.close()


def cmd_docs(args) -> int:
    store = _store(args)
    try:
        for row in store.list_documents(include_withdrawn=args.all):
            print("%-20s %-8s %-5s %6d chars %5d phrases  %s" % (
                row["id"], row["status"], "EMPTY" if row["is_empty"] else "",
                row["char_count"], row["phrase_count"], row["title"]))
        return 0
    finally:
        store.close()


def cmd_tasks(args) -> int:
    store = _store(args)
    try:
        for task in store.list_tasks():
            print("%-22s %-10s %-7s thr=%.2f done=%d/%d  %s" % (
                task["id"], task["status"], task["task_mode"], task["threshold"],
                task["done"], task["total"], task["note"] or ""))
        return 0
    finally:
        store.close()


def cmd_rules(args) -> int:
    _print(textrules.describe_rules(textrules.TOKENIZER_ID, textrules.PHRASES_ID,
                                    args.phrase_n))
    return 0


def cmd_serve(args) -> int:
    from .app import serve
    serve(host=args.host, port=args.port, db_path=Path(args.db))
    return 0


def cmd_demo(args) -> int:
    from . import demo
    store = _store(args)
    try:
        result = demo.seed(store, reset=args.reset, db_path=args.db)
        _print(result)
        return 0
    finally:
        store.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="corpuslake",
                                description="Local text-corpus dedupe and grouping.")
    p.add_argument("--db", default=str(DEFAULT_DB),
                   help="SQLite database path (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create the database")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("import", help="import a file or directory")
    s.add_argument("path")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("index", help="build an index task")
    s.add_argument("--threshold", type=float, default=0.6)
    s.add_argument("--phrase-n", type=int, default=textrules.DEFAULT_PHRASE_N)
    s.add_argument("--mode", choices=("exact", "approx"), default="exact",
                   help="exact (default, provably complete) or approx (MinHash)")
    s.add_argument("--num-perm", type=int, default=128)
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("verify", help="verify an index with full pairwise comparison")
    s.add_argument("--task")
    s.add_argument("--sample", type=int, default=verification.MAX_VERIFY_DOCS,
                   help="documents to compare pairwise when the snapshot is larger "
                        "(default %(default)s, deterministic sample)")
    s.add_argument("--full", action="store_true",
                   help="verify every document, however large the snapshot")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("export", help="export edges/groups/manifest")
    s.add_argument("--task")
    s.add_argument("--format", choices=("bundle", "manifest", "edges.csv", "links.nq"),
                   default="bundle")
    s.add_argument("--out", default=str(DEFAULT_DB.parent / "exports" / "corpuslake.zip"))
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("groups", help="rebuild groups")
    s.add_argument("--task")
    s.add_argument("--basis", choices=("accepted", "confirmed"), default="accepted",
                   help="accepted (default): every edge not marked keep-separate; "
                        "confirmed: only edges explicitly marked same-group "
                        "(an opt-in stricter grouping)")
    s.set_defaults(func=cmd_groups)

    s = sub.add_parser("docs", help="list documents")
    s.add_argument("--all", action="store_true", help="include withdrawn documents")
    s.set_defaults(func=cmd_docs)

    s = sub.add_parser("tasks", help="list index tasks")
    s.set_defaults(func=cmd_tasks)

    s = sub.add_parser("rules", help="show the active rule descriptions")
    s.add_argument("--phrase-n", type=int, default=textrules.DEFAULT_PHRASE_N)
    s.set_defaults(func=cmd_rules)

    s = sub.add_parser("serve", help="start the review UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("demo", help="seed a demonstration corpus")
    s.add_argument("--reset", action="store_true")
    s.set_defaults(func=cmd_demo)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
