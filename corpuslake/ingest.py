"""Bulk corpus ingestion for the CLI.

Accepted formats
----------------
* ``.txt`` / ``.md`` / ``.rst`` - one document per file (UTF-8, BOM tolerated).
* ``.jsonl`` / ``.ndjson`` - one object per line; the text field is ``text``
  and the title is ``title`` (falling back to ``id`` or the filename + line).
* ``.csv`` - header row required; uses the ``text`` and ``title`` columns.
* ``.json`` - either a list of objects or an object whose ``documents`` key is
  a list of objects, same field convention as JSONL.

Files are read as untrusted data: nothing in the corpus is executed or
imported. A per-document error is recorded and the import continues; the
command exits non-zero only if every document failed.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from .store import Store

TEXT_SUFFIXES = {".txt", ".md", ".rst", ".text"}
JSONL_SUFFIXES = {".jsonl", ".ndjson"}
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | JSONL_SUFFIXES | {".csv", ".json"}


@dataclass
class ImportItem:
    title: str
    text: str
    source_ref: str


@dataclass
class ImportReport:
    imported: int = 0
    skipped: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)
    created: list[dict[str, Any]] = field(default_factory=list)
    duplicates: list[dict[str, Any]] = field(default_factory=list)


def _iter_jsonl(raw: str) -> Iterator[dict[str, Any]]:
    for line_no, line in enumerate(io.StringIO(raw), 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("line %d: %s" % (line_no, exc)) from None
        if not isinstance(obj, dict):
            raise ValueError("line %d: expected a JSON object" % line_no)
        yield obj


def _iter_csv(raw: str) -> Iterator[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(raw))
    if reader.fieldnames is None:
        raise ValueError("CSV needs a header row with a 'text' column")
    lower = {name.lower(): name for name in reader.fieldnames}
    if "text" not in lower:
        raise ValueError("CSV needs a 'text' column; found %s" % reader.fieldnames)
    text_col = lower["text"]
    title_col = lower.get("title")
    for row_no, row in enumerate(reader, 2):
        yield {"text": row.get(text_col, ""),
               "title": row.get(title_col, "") if title_col else "",
               "_row": row_no}


def items_from_file(path: Path) -> list[ImportItem]:
    name = path.name
    if path.suffix.lower() in TEXT_SUFFIXES:
        raw = path.read_bytes().decode("utf-8-sig", errors="replace")
        return [ImportItem(title=path.stem, text=raw, source_ref=str(path))]
    if not path.suffix.lower() in SUPPORTED_SUFFIXES:
        raise ValueError("unsupported file type: %s" % path.suffix)
    raw = path.read_bytes().decode("utf-8-sig", errors="replace")
    records: Iterable[dict[str, Any]]
    if path.suffix.lower() in JSONL_SUFFIXES:
        records = _iter_jsonl(raw)
    elif path.suffix.lower() == ".csv":
        records = _iter_csv(raw)
    else:
        data = json.loads(raw)
        if isinstance(data, dict):
            data = data.get("documents", [data])
        if not isinstance(data, list):
            raise ValueError("JSON must be a list of objects or {'documents': [...]}")
        records = data
    out: list[ImportItem] = []
    for idx, obj in enumerate(records, 1):
        if not isinstance(obj, dict):
            raise ValueError("record %d is not an object" % idx)
        text = obj.get("text", obj.get("content", ""))
        if not isinstance(text, str):
            raise ValueError("record %d: 'text' must be a string" % idx)
        title = obj.get("title") or obj.get("id") or ("%s#%d" % (name, idx))
        out.append(ImportItem(title=str(title), text=text, source_ref=str(path)))
    return out


def import_file(store: Store, path: Path) -> ImportReport:
    report = ImportReport()
    try:
        items = items_from_file(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        report.errors.append({"file": str(path), "error": str(exc)})
        return report
    for item in items:
        try:
            row, created = store.add_document(
                item.title, item.text, source="file", source_ref=item.source_ref)
        except Exception as exc:  # noqa: BLE001 - report and continue
            report.errors.append({"file": str(path), "title": item.title,
                                  "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        if created:
            report.imported += 1
            report.created.append({"doc_id": row["id"], "title": row["title"],
                                   "source_ref": item.source_ref})
        else:
            report.skipped += 1
            report.duplicates.append({"doc_id": row["id"], "title": row["title"]})
    return report


def import_directory(store: Store, directory: Path) -> dict[str, Any]:
    if not directory.is_dir():
        raise ValueError("not a directory: %s" % directory)
    combined = {"imported": 0, "skipped": 0, "errors": [], "files": []}
    files = sorted(p for p in directory.rglob("*")
                   if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES)
    for path in files:
        report = import_file(store, path)
        combined["imported"] += report.imported
        combined["skipped"] += report.skipped
        combined["errors"].extend(report.errors)
        combined["files"].append({
            "file": str(path), "imported": report.imported,
            "skipped": report.skipped, "errors": len(report.errors),
        })
    return combined
