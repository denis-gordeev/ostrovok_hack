import bz2
import gzip
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import faiss
import numpy as np
from graph_retriever.strategies import Eager
from langchain_graph_retriever import GraphRetriever

from scripts.wiki_ingest import run, sql_rows, value
from wiki_retrieval import WikipediaAdapter, wikipedia_edges
from wiki_store import add_text, connect, set_meta


class WikipediaTests(unittest.TestCase):
    def test_dump_selection_redirects_depth_images_and_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            date = "20261001"

            def sql(name, fields, rows):
                text = (
                    "CREATE TABLE `"
                    + name
                    + "` (\n"
                    + "".join(" `" + n + "` text,\n" for n in fields)
                    + ");\nINSERT INTO `"
                    + name
                    + "` VALUES\n"
                    + ",\n".join(rows)
                    + ";\n"
                )
                with gzip.open(folder / f"ruwiki-{date}-{name}.sql.gz", "wt") as f:
                    f.write(text)

            sql(
                "page",
                ["page_id", "page_namespace", "page_title"],
                [
                    "(1,0,'Казань')",
                    "(2,0,'Кул-Шариф')",
                    "(3,0,'Архитектура')",
                    "(4,0,'Третий_переход')",
                    "(100,0,'Алиас')",
                ],
            )
            sql(
                "linktarget",
                ["lt_id", "lt_namespace", "lt_title"],
                [
                    "(10,0,'Алиас')",
                    "(11,0,'Архитектура')",
                    "(12,0,'Третий_переход')",
                    "(13,0,'Не_существует')",
                    "(20,6,'Мечеть.jpg')",
                ],
            )
            sql(
                "redirect",
                ["rd_from", "rd_namespace", "rd_title", "rd_interwiki", "rd_fragment"],
                ["(100,0,'Кул-Шариф','','')"],
            )
            sql(
                "pagelinks",
                ["pl_from", "pl_from_namespace", "pl_target_id"],
                ["(1,0,10)", "(1,0,13)", "(2,0,11)", "(3,0,12)"],
            )
            sql("imagelinks", ["il_from", "il_from_namespace", "il_target_id"], ["(2,0,20)", "(4,0,20)"])
            xml = '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">'
            for pid, title in [(1, "Казань"), (2, "Кул-Шариф"), (3, "Архитектура"), (4, "Третий переход")]:
                xml += f"<page><title>{title}</title><id>{pid}</id><revision><id>{pid+1000}</id><timestamp>2026-10-01T00:00:00Z</timestamp><text>Проверенный текст о [[Казань|Казани]].</text></revision></page>"
            with bz2.open(folder / f"ruwiki-{date}-pages-articles-multistream.xml.bz2", "wb") as f:
                f.write((xml + "</mediawiki>").encode())
            (folder / "dumpstatus.json").write_text("{}")
            run(folder, date, "Казань")
            run(folder, date, "Казань")
            with connect(folder / "corpus.sqlite", readonly=True) as db:
                self.assertEqual(
                    [(r["id"], r["depth"]) for r in db.execute("SELECT id,depth FROM articles ORDER BY id")],
                    [(1, 0), (2, 1), (3, 2)],
                )
                self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM edges ORDER BY src")], [(1, 2), (2, 3)])
                self.assertEqual([tuple(r) for r in db.execute("SELECT * FROM images")], [(2, "Мечеть.jpg")])
                self.assertIn("Казани", db.execute("SELECT text FROM articles WHERE id=2").fetchone()[0])

    def test_mysql_strings(self):
        self.assertEqual(value(r"'О\'Брайен, (город)\nстрока'"), "О'Брайен, (город)\nстрока")

    def test_replacing_article_removes_stale_full_text_matches(self):
        from wiki_store import expression

        with tempfile.TemporaryDirectory() as temp:
            with connect(Path(temp) / "corpus.sqlite") as db:
                add_text(db, 1, "Статья", "Старый велосипед")
                add_text(db, 1, "Статья", "Новый автомобиль")
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH ?", (expression("велосипед"),)
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    db.execute(
                        "SELECT count(*) FROM chunk_fts WHERE chunk_fts MATCH ?", (expression("автомобиль"),)
                    ).fetchone()[0],
                    1,
                )

    def test_real_graph_retriever_traverses_sql_edges(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            db = connect(folder / "corpus.sqlite")
            for pid, title, text in [
                (1, "Казань", "Кремль Казани"),
                (2, "Кул-Шариф", "Мечеть и архитектура"),
                (3, "Борщ", "Суп из свеклы"),
            ]:
                db.execute(
                    "INSERT INTO articles(id,title,depth,revision,text) VALUES (?,?,?,?,?)",
                    (pid, title, min(pid - 1, 2), pid + 100, text),
                )
                add_text(db, pid, title, text)
            db.execute("INSERT INTO edges VALUES(1,2)")
            for key, val in {
                "snapshot": "20261001",
                "retrieved_at": "2026-10-27",
                "text_license": "CC BY-SA 4.0",
                "counts": {"articles": 3, "chunks": 3, "edges": 1},
            }.items():
                set_meta(db, key, val)
            db.commit()
            db.close()
            vectors = [[1.0, 0.0, 0.0], [0.9, 0.1, 0.0], [0.0, 1.0, 0.0]]
            vectors = np.asarray(vectors, dtype="float32")
            faiss.normalize_L2(vectors)
            vd = sqlite3.connect(folder / "vectors.sqlite")
            vd.execute("CREATE TABLE vectors(id INTEGER PRIMARY KEY,page_id INTEGER,vector BLOB)")
            vd.executemany(
                "INSERT INTO vectors VALUES (?,?,?)", [(i + 1, i + 1, v.tobytes()) for i, v in enumerate(vectors)]
            )
            vd.commit()
            vd.close()
            index = faiss.IndexIDMap2(faiss.IndexHNSWFlat(3, 4, faiss.METRIC_INNER_PRODUCT))
            index.add_with_ids(vectors, np.arange(1, 4, dtype="int64"))
            faiss.write_index(index, str(folder / "index.faiss"))
            (folder / "index.json").write_text(json.dumps({"fingerprint": "fake", "model": "fixture"}))

            class FakeEncoder:
                fingerprint = "fake"

                def embed_query(self, query):
                    return [1.0, 0.0, 0.0]

            adapter = WikipediaAdapter(folder, FakeEncoder())
            retriever = GraphRetriever(
                store=adapter, edges=wikipedia_edges, strategy=Eager(start_k=1, select_k=2, adjacent_k=1, max_depth=1)
            )
            docs = retriever.invoke("кремль")
            self.assertEqual({d.metadata["title"] for d in docs}, {"Казань", "Кул-Шариф"})
            self.assertTrue(all("oldid=" in d.metadata["revision_url"] for d in docs))
            # A match deep in an article must survive even when only its lead is in FAISS.
            with connect(folder / "corpus.sqlite") as db:
                db.execute(
                    "INSERT INTO chunks(page_id,position,text) VALUES(3,1,?)", ("Уникальный музей в глубине статьи",)
                )
                cid = db.execute("SELECT max(id) FROM chunks").fetchone()[0]
                from wiki_store import terms

                db.execute(
                    "INSERT INTO chunk_fts VALUES (?,?,?,?)",
                    (terms("Уникальный музей"), terms("Уникальный музей в глубине статьи"), cid, 3),
                )
            _, contents = adapter.search_with_embedding("Уникальный музей", k=3)
            self.assertTrue(any("глубине" in content.content for content in contents))


if __name__ == "__main__":
    unittest.main()
