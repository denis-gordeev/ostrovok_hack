"""Resume local RuBERT embeddings and publish a FAISS graph-retrieval index."""

import argparse, hashlib, json, sqlite3, sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import faiss, numpy as np
from wiki_embeddings import LocalRuBertEmbeddings
from wiki_store import connect, metadata


def build(folder, url=None):
    folder = Path(folder)
    encoder = LocalRuBertEmbeddings(url, folder / "encoder-cache")
    faiss.omp_set_num_threads(1)
    corpus = connect(folder / "corpus.sqlite", readonly=True)
    meta = metadata(corpus)
    identity = hashlib.sha256(
        json.dumps({k: meta[k] for k in ["snapshot", "root", "counts"]}, sort_keys=True).encode()
    ).hexdigest()
    db = sqlite3.connect(folder / "vectors.sqlite", timeout=120)
    db.executescript(
        "CREATE TABLE IF NOT EXISTS vectors(id INTEGER PRIMARY KEY,page_id INTEGER,vector BLOB);CREATE INDEX IF NOT EXISTS vectors_page ON vectors(page_id);CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT);"
    )
    settings = {"model": encoder.fingerprint, "corpus": identity, "index_mode": "article-lead-1200-v1"}
    old = dict(db.execute("SELECT * FROM settings"))
    if old and old != settings:
        raise ValueError("Different corpus/model: use a separate directory for new vectors")
    db.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)", settings.items())
    db.commit()
    last = db.execute("SELECT coalesce(max(id),0) FROM vectors").fetchone()[0]
    total = corpus.execute("SELECT count(*) FROM chunks WHERE position=0").fetchone()[0]
    started = time.monotonic()
    new_count = 0
    while True:
        rows = corpus.execute(
            "SELECT c.id,c.page_id,c.text,a.title FROM chunks c JOIN articles a ON a.id=c.page_id WHERE c.position=0 AND c.id>? ORDER BY c.id LIMIT 16",
            (last,),
        ).fetchall()
        if not rows:
            break
        vectors = encoder.embed_documents([r["title"] + "\n" + r["text"][:1200] for r in rows])
        db.executemany(
            "INSERT INTO vectors VALUES (?,?,?)",
            [(r["id"], r["page_id"], np.asarray(v, dtype="<f4").tobytes()) for r, v in zip(rows, vectors)],
        )
        db.commit()
        last = rows[-1]["id"]
        new_count += len(rows)
        if new_count % 256 == 0:
            count = db.execute("SELECT count(*) FROM vectors").fetchone()[0]
            status = {
                "phase": "embeddings",
                "indexed": count,
                "total": total,
                "chunks_per_second": round(new_count / (time.monotonic() - started), 2),
            }
            tmp = folder / "embedding-status.tmp"
            tmp.write_text(json.dumps(status))
            tmp.replace(folder / "embedding-status.json")
            print(json.dumps(status), flush=True)
    if db.execute("SELECT count(*) FROM vectors").fetchone()[0] != total:
        raise ValueError("Incomplete vector set")
    print("Building FAISS", total, flush=True)
    index = faiss.IndexIDMap2(faiss.IndexHNSWFlat(encoder.info["dimensions"], 16, faiss.METRIC_INNER_PRODUCT))
    faiss.downcast_index(index.index).hnsw.efConstruction = 64
    cur = db.execute("SELECT id,vector FROM vectors ORDER BY id")
    while rows := cur.fetchmany(2000):
        ids = np.asarray([r[0] for r in rows], dtype="int64")
        matrix = np.stack([np.frombuffer(r[1], dtype="<f4") for r in rows])
        index.add_with_ids(matrix, ids)
    temporary = folder / "index.faiss.tmp"
    faiss.write_index(index, str(temporary))
    temporary.replace(folder / "index.faiss")
    info = {
        **encoder.info,
        "fingerprint": encoder.fingerprint,
        "corpus_identity": identity,
        "vectors": total,
        "mode": "one lead vector per article (title + first 1200 characters); full text remains searchable with FTS",
    }
    tmp = folder / "index.json.tmp"
    tmp.write_text(json.dumps(info))
    tmp.replace(folder / "index.json")
    (folder / "embedding-status.json").write_text(json.dumps({"phase": "validating", "indexed": total, "total": total}))
    from scripts.wiki_check import check

    try:
        check(folder, encoder)
    except Exception:
        (folder / "index.json").unlink(missing_ok=True)
        raise
    (folder / "embedding-status.json").write_text(json.dumps({"phase": "complete", "indexed": total, "total": total}))
    print("COMPLETE", total, flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--folder", required=True)
    p.add_argument("--url")
    a = p.parse_args()
    try:
        build(a.folder, a.url)
    except Exception as exc:
        (Path(a.folder) / "embedding-status.json").write_text(
            json.dumps({"phase": "failed", "error": type(exc).__name__ + ": " + str(exc)})
        )
        raise
