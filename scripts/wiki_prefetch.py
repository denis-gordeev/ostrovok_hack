"""Optional overlap of local embeddings with corpus extraction; cache only.

No corpus/index is published by this helper. The final indexer verifies all rows.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from wiki_embeddings import LocalRuBertEmbeddings
from wiki_store import connect


def prefetch(folder, url):
    folder = Path(folder)
    encoder = LocalRuBertEmbeddings(url, folder / "encoder-cache")
    last = 0
    count = 0
    while not (folder / "corpus.sqlite").exists():
        path = folder / "building.sqlite"
        if not path.exists():
            time.sleep(3)
            continue
        try:
            with connect(path, readonly=True) as db:
                rows = db.execute(
                    "SELECT c.id,c.text,a.title FROM chunks c JOIN articles a ON a.id=c.page_id WHERE c.position=0 AND c.id>? ORDER BY c.id LIMIT 16",
                    (last,),
                ).fetchall()
        except sqlite3.OperationalError:
            time.sleep(3)
            continue
        if not rows:
            time.sleep(3)
            continue
        encoder.embed_documents([r["title"] + "\n" + r["text"][:1200] for r in rows])
        last = rows[-1]["id"]
        count += len(rows)
        if count % 256 == 0:
            print("PREFETCHED", count, "last_chunk", last, flush=True)
    print("Corpus published; final indexer completes remaining vectors", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder", required=True)
    p.add_argument("--url", required=True)
    a = p.parse_args()
    prefetch(a.folder, a.url)
