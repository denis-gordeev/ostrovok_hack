"""Stream pinned Wikipedia SQL/XML dumps into a two-hop Kazan corpus.

Run after wiki_download.py. Stages are restartable; corpus publication is atomic.
No arbitrary SQL from a dump is executed, no LLM or embedding service is called.
"""

import argparse
import bz2
import gzip
import json
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mwparserfromhell

from wiki_store import add_text, connect, set_meta

TUPLE = re.compile(r"\((?:[^'()]|'(?:[^'\\]|\\.)*')*\)")
TOKEN = re.compile(r"'(?:[^'\\]|\\.)*'|NULL|-?\d+(?:\.\d+)?")
NUMERIC = re.compile(r"\((\d+),(\d+),(-?\d+)\)")
ESC = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "b": "\b", "Z": "\x1a"}


def value(raw):
    if raw == "NULL":
        return None
    if not raw.startswith("'"):
        return int(raw)
    return re.sub(r"\\(.)", lambda m: ESC.get(m[1], m[1]), raw[1:-1])


def sql_rows(path):
    columns = []
    in_insert = False
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if line.startswith("CREATE TABLE"):
                columns = []
            if line.lstrip().startswith("`"):
                columns.append(line.strip().split("`")[1])
            if line.startswith("INSERT INTO "):
                in_insert = True
                body = line.split("VALUES", 1)[1]
            elif in_insert:
                body = line
            else:
                continue
            if not columns:
                raise ValueError(f"Missing SQL schema: {path}")
            if body.rstrip().endswith(";"):
                in_insert = False
            for match in TUPLE.finditer(body):
                row = TOKEN.findall(match[0])
                if len(row) != len(columns):
                    raise ValueError(f"Unsupported SQL tuple in {path}: expected {len(columns)} fields, got {len(row)}")
                yield columns, row


def numeric_links(path, source_ids):
    import numpy as np

    columns = []
    pattern = re.compile(rb"\((\d+),(\d+),(-?\d+)\)")
    with gzip.open(path, "rb") as stream:
        for line in stream:
            if line.lstrip().startswith(b"`"):
                columns.append(line.strip().split(b"`")[1].decode())
            if line.startswith(b"INSERT INTO "):
                break
        else:
            return
        if set(columns) != {"pl_from", "pl_target_id", "pl_from_namespace"}:
            raise ValueError(f"Unsupported pagelinks schema: {columns}")
        indices = [columns.index(n) for n in ["pl_from", "pl_target_id", "pl_from_namespace"]]
        if not source_ids:
            return
        single = len(source_ids) == 1 and indices[0] == 0
        if single:
            root = next(iter(source_ids))
            pattern = re.compile(rb"\((" + str(root).encode() + rb"),(\d+),(-?\d+)\)")
        else:
            max_id = max(source_ids)
            membership = np.zeros(max_id + 1, dtype=bool)
            membership[list(source_ids)] = True
        translation = bytes.maketrans(b"(),;\n\r\t", b"       ")
        buffer = line.split(b"VALUES", 1)[1]
        while True:
            block = stream.read(8 * 1024 * 1024)
            buffer += block
            end = buffer.rfind(b")")
            complete, buffer = (buffer[: end + 1], buffer[end + 1 :]) if end >= 0 else (b"", buffer)
            if complete:
                if not single and re.fullmatch(rb"[\s(),;0-9-]+", complete):
                    values = np.fromstring(complete.translate(translation), dtype=np.int64, sep=" ").reshape(-1, 3)
                    sources = values[:, indices[0]]
                    mask = (sources <= max_id) & membership[np.minimum(sources, max_id)] & (values[:, indices[2]] == 0)
                    for row in values[mask]:
                        yield int(row[indices[0]]), int(row[indices[1]])
                else:
                    for m in pattern.finditer(complete):
                        src = int(m[indices[0] + 1])
                        if src in source_ids and int(m[indices[2] + 1]) == 0:
                            yield src, int(m[indices[1] + 1])
            if not block:
                break


def run(folder, snapshot, root_title):
    folder = Path(folder)
    if (folder / "corpus.sqlite").exists():
        print("Corpus already published; use a new snapshot directory to rebuild", flush=True)
        return
    inventory = sqlite3.connect(folder / "inventory.sqlite")
    inventory.row_factory = sqlite3.Row
    inventory.executescript(
        "CREATE TABLE IF NOT EXISTS stages(name TEXT PRIMARY KEY); CREATE TABLE IF NOT EXISTS page(id INTEGER PRIMARY KEY,title TEXT UNIQUE); CREATE TABLE IF NOT EXISTS target(id INTEGER PRIMARY KEY,page_id INTEGER); CREATE TABLE IF NOT EXISTS redirects(src INTEGER PRIMARY KEY,dst INTEGER);"
    )

    def path(table):
        return folder / f"ruwiki-{snapshot}-{table}.sql.gz"

    def status(phase, **kwargs):
        data = {"phase": phase, "at": datetime.now(timezone.utc).isoformat(), **kwargs}
        tmp = folder / "status.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(folder / "status.json")
        print(json.dumps(data, ensure_ascii=False), flush=True)

    def done(name):
        return inventory.execute("SELECT 1 FROM stages WHERE name=?", (name,)).fetchone() is not None

    def finish(name):
        inventory.execute("INSERT OR IGNORE INTO stages VALUES (?)", (name,))
        inventory.commit()

    if not done("page"):
        status("page")
        inventory.execute("DELETE FROM page")
        batch = []
        for columns, row in sql_rows(path("page")):
            if int(row[columns.index("page_namespace")]) == 0:
                batch.append((int(row[columns.index("page_id")]), value(row[columns.index("page_title")])))
            if len(batch) >= 20000:
                inventory.executemany("INSERT INTO page VALUES (?,?)", batch)
                inventory.commit()
                batch = []
        inventory.executemany("INSERT INTO page VALUES (?,?)", batch)
        finish("page")
    if not done("target"):
        status("target")
        inventory.executescript(
            "DROP TABLE IF EXISTS raw_target; CREATE TABLE raw_target(id INTEGER PRIMARY KEY,title TEXT); DELETE FROM target;"
        )
        batch = []
        for columns, row in sql_rows(path("linktarget")):
            if int(row[columns.index("lt_namespace")]) == 0:
                batch.append((int(row[columns.index("lt_id")]), value(row[columns.index("lt_title")])))
            if len(batch) >= 20000:
                inventory.executemany("INSERT INTO raw_target VALUES (?,?)", batch)
                inventory.commit()
                batch = []
        inventory.executemany("INSERT INTO raw_target VALUES (?,?)", batch)
        inventory.execute("INSERT INTO target SELECT r.id,p.id FROM raw_target r JOIN page p ON p.title=r.title")
        inventory.execute("DROP TABLE raw_target")
        finish("target")
    if not done("redirects"):
        status("redirects")
        inventory.executescript(
            "DROP TABLE IF EXISTS raw_redirect; CREATE TABLE raw_redirect(src INTEGER PRIMARY KEY,title TEXT); DELETE FROM redirects;"
        )
        batch = []
        for columns, row in sql_rows(path("redirect")):
            if int(row[columns.index("rd_namespace")]) == 0 and value(row[columns.index("rd_interwiki")]) in ("", None):
                batch.append((int(row[columns.index("rd_from")]), value(row[columns.index("rd_title")])))
            if len(batch) >= 20000:
                inventory.executemany("INSERT INTO raw_redirect VALUES (?,?)", batch)
                inventory.commit()
                batch = []
        inventory.executemany("INSERT INTO raw_redirect VALUES (?,?)", batch)
        inventory.execute("INSERT INTO redirects SELECT r.src,p.id FROM raw_redirect r JOIN page p ON p.title=r.title")
        inventory.execute("DROP TABLE raw_redirect")
        finish("redirects")

    @lru_cache(maxsize=200000)
    def canonical(pid):
        visited = set()
        while pid not in visited:
            visited.add(pid)
            row = inventory.execute("SELECT dst FROM redirects WHERE src=?", (pid,)).fetchone()
            if not row:
                return pid
            pid = row[0]
        return None

    @lru_cache(maxsize=200000)
    def target(tid):
        row = inventory.execute("SELECT page_id FROM target WHERE id=?", (tid,)).fetchone()
        return canonical(row[0]) if row else None

    root_row = inventory.execute("SELECT id FROM page WHERE title=?", (root_title.replace(" ", "_"),)).fetchone()
    if not root_row:
        raise ValueError("Root article is absent from snapshot")
    root = canonical(root_row[0])
    corpus_path = folder / "building.sqlite"
    db = connect(corpus_path)
    db.execute("PRAGMA journal_mode=WAL")
    if not done("selection"):
        db.execute("DELETE FROM articles")
        selected = {root: (0, None)}
        frontier = {root}
        for depth in (1, 2):
            status("select", depth=depth, selected=len(selected))
            new = {}
            for src, tid in numeric_links(path("pagelinks"), frontier):
                dst = target(tid)
                if dst is not None and dst not in selected:
                    new[dst] = (depth, min(src, new.get(dst, (depth, src))[1]))
            selected.update(new)
            frontier = set(new)
        for pid, (depth, parent) in selected.items():
            title = inventory.execute("SELECT title FROM page WHERE id=?", (pid,)).fetchone()[0].replace("_", " ")
            db.execute("INSERT INTO articles(id,title,depth,parent) VALUES (?,?,?,?)", (pid, title, depth, parent))
        set_meta(db, "snapshot", snapshot)
        set_meta(db, "root", root_title)
        set_meta(db, "root_id", root)
        set_meta(db, "text_license", "https://creativecommons.org/licenses/by-sa/4.0/")
        set_meta(db, "retrieved_at", datetime.now(timezone.utc).isoformat())
        db.commit()
        finish("selection")
    selected = {r["id"] for r in db.execute("SELECT id FROM articles")}
    if not done("edges"):
        status("edges", selected=len(selected))
        db.execute("DELETE FROM edges")
        batch = []
        for src, tid in numeric_links(path("pagelinks"), selected):
            dst = target(tid)
            if dst in selected and dst != src:
                batch.append((src, dst))
            if len(batch) >= 20000:
                db.executemany("INSERT OR IGNORE INTO edges VALUES (?,?)", batch)
                db.commit()
                batch = []
        db.executemany("INSERT OR IGNORE INTO edges VALUES (?,?)", batch)
        db.commit()
        finish("edges")
    if not done("texts"):
        pending = {r[0] for r in db.execute("SELECT id FROM articles WHERE text IS NULL")}
        existing = len(selected) - len(pending)
        status("texts", selected=len(selected), indexed=existing)
        count = 0
        xml_path = folder / f"ruwiki-{snapshot}-pages-articles-multistream.xml.bz2"
        with bz2.open(xml_path, "rb") as stream:
            context = ET.iterparse(stream, events=("start", "end"))
            _, xml_root = next(context)
            ns = "{http://www.mediawiki.org/xml/export-0.11/}"
            # Namespace varies across dump versions.
            ns = xml_root.tag.partition("}")[0] + "}" if "}" in xml_root.tag else ""
            for event, element in context:
                if event != "end" or element.tag != ns + "page":
                    continue
                pid = int(element.findtext(ns + "id"))
                if pid in pending:
                    rev = element.find(ns + "revision")
                    raw = rev.findtext(ns + "text") or ""
                    title = element.findtext(ns + "title")
                    text = mwparserfromhell.parse(raw).strip_code(normalize=True, collapse=True).strip()
                    db.execute(
                        "UPDATE articles SET revision=?,modified=?,text=?,wikitext_gz=? WHERE id=?",
                        (
                            int(rev.findtext(ns + "id")),
                            rev.findtext(ns + "timestamp"),
                            text,
                            gzip.compress(raw.encode()),
                            pid,
                        ),
                    )
                    add_text(db, pid, title, text)
                    count += 1
                    if count % 500 == 0:
                        db.commit()
                        status("texts", indexed=existing + count, selected=len(selected))
                element.clear()
                xml_root.clear()
        db.commit()
        finish("texts")
    if not done("images"):
        status("images")
        db.execute("DELETE FROM images")
        batch = []
        db.executescript(
            "DROP TABLE IF EXISTS image_targets; CREATE TABLE image_targets(page_id INTEGER,target_id INTEGER,PRIMARY KEY(page_id,target_id));"
        )
        for columns, row in sql_rows(path("imagelinks")):
            pid = int(row[columns.index("il_from")])
            if pid not in selected:
                continue
            if "il_to" in columns:
                batch.append((pid, value(row[columns.index("il_to")])))
            else:
                db.execute(
                    "INSERT OR IGNORE INTO image_targets VALUES (?,?)", (pid, int(row[columns.index("il_target_id")]))
                )
            if len(batch) >= 20000:
                db.executemany("INSERT OR IGNORE INTO images VALUES (?,?)", batch)
                db.commit()
                batch = []
        needed = {r[0] for r in db.execute("SELECT DISTINCT target_id FROM image_targets")}
        db.execute("CREATE INDEX IF NOT EXISTS image_targets_id ON image_targets(target_id)")
        if needed:
            for columns, row in sql_rows(path("linktarget")):
                tid = int(row[columns.index("lt_id")])
                if tid in needed and int(row[columns.index("lt_namespace")]) == 6:
                    db.execute(
                        "INSERT OR IGNORE INTO images SELECT page_id,? FROM image_targets WHERE target_id=?",
                        (value(row[columns.index("lt_title")]), tid),
                    )
            db.execute("DROP TABLE image_targets")
        db.executemany("INSERT OR IGNORE INTO images VALUES (?,?)", batch)
        db.commit()
        finish("images")
    missing = [r["id"] for r in db.execute("SELECT id FROM articles WHERE text IS NULL")]
    if root in missing:
        raise ValueError("Root text missing; refusing publication")
    counts = {
        t: db.execute("SELECT count(*) FROM " + t).fetchone()[0] for t in ["articles", "chunks", "edges", "images"]
    }
    set_meta(db, "counts", counts)
    set_meta(db, "missing_text_ids", missing)
    set_meta(db, "build_status", "complete" if not missing else "complete_with_missing_texts")
    manifest = json.loads((folder / "dumpstatus.json").read_text())
    set_meta(db, "dump_manifest", manifest)
    db.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('optimize')")
    db.commit()
    result = db.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise ValueError(result)
    checkpoint = db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if checkpoint[0]:
        raise ValueError("Corpus checkpoint busy; retry publication")
    db.close()
    inventory.close()
    # Readers keep the old inode until their request completes.
    corpus_path.replace(folder / "corpus.sqlite")
    status("complete", counts=counts, missing_texts=len(missing))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder", required=True)
    p.add_argument("--snapshot", default="20261001")
    p.add_argument("--root", default="Казань")
    a = p.parse_args()
    try:
        run(a.folder, a.snapshot, a.root)
    except Exception as exc:
        error = {
            "phase": "failed",
            "error": type(exc).__name__ + ": " + str(exc),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        (Path(a.folder) / "status.json").write_text(json.dumps(error, ensure_ascii=False))
        print("FAILED", type(exc).__name__, str(exc), flush=True)
        raise
