"""Disk corpus and deterministic Russian lexical features; no model calls."""

import json, re, sqlite3, threading
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote
import snowballstemmer

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles(id INTEGER PRIMARY KEY,title TEXT UNIQUE,depth INTEGER,parent INTEGER,revision INTEGER,modified TEXT,text TEXT,wikitext_gz BLOB);
CREATE TABLE IF NOT EXISTS edges(src INTEGER,dst INTEGER,PRIMARY KEY(src,dst));
CREATE INDEX IF NOT EXISTS edges_dst ON edges(dst);
CREATE TABLE IF NOT EXISTS images(page_id INTEGER,name TEXT,PRIMARY KEY(page_id,name));
CREATE TABLE IF NOT EXISTS chunks(id INTEGER PRIMARY KEY,page_id INTEGER,position INTEGER,text TEXT);
CREATE INDEX IF NOT EXISTS chunks_page ON chunks(page_id,position);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(title,body,chunk_id UNINDEXED,page_id UNINDEXED,tokenize='unicode61');
CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT);
"""
STEMMER = snowballstemmer.stemmer("russian")
STEM_LOCK = threading.Lock()
STOP = set(
    "и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из ему теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем сказать всех никогда сегодня можно при об другой расскажи рассказать про найти информация пожалуйста такой".split()
)


@lru_cache(maxsize=100000)
def stem(word):
    with STEM_LOCK:
        return STEMMER.stemWord(word)


def tokens(text):
    return [
        stem(w)
        for w in re.findall(r"[^\W_]+", text.lower().replace("ё", "е"), re.UNICODE)
        if len(w) > 1 and w not in STOP
    ][:50000]


def terms(text):
    return " ".join(tokens(text))


def expression(query):
    words = list(dict.fromkeys(tokens(query)))[:20]
    return " OR ".join('"' + w + '"' for w in words)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path, readonly=False):
    path = Path(path)
    if readonly:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30, factory=ClosingConnection)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path, timeout=60, factory=ClosingConnection)
        con.executescript(SCHEMA)
    con.row_factory = sqlite3.Row
    return con


def article_url(title):
    return "https://ru.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="")


def image_links(name):
    encoded = quote(name.replace(" ", "_"), safe="")
    return {
        "name": name,
        "image_url": "https://ru.wikipedia.org/wiki/Special:Redirect/file/" + encoded,
        "description_url": article_url("Файл:" + name),
        "url_kind": "MediaWiki file redirect; file existence and per-file license must be checked on description page",
    }


def set_meta(con, key, value):
    con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, json.dumps(value, ensure_ascii=False)))


def metadata(con):
    return {r["key"]: json.loads(r["value"]) for r in con.execute("SELECT * FROM metadata")}


def add_text(con, page_id, title, text, chunk_size=2400):
    # The FTS page_id is unindexed. Never scan the growing FTS table for every
    # brand-new article; only remove old fragments when an article is replaced.
    if con.execute("SELECT 1 FROM chunks WHERE page_id=? LIMIT 1", (page_id,)).fetchone():
        con.execute("DELETE FROM chunk_fts WHERE page_id=?", (page_id,))
        con.execute("DELETE FROM chunks WHERE page_id=?", (page_id,))
    # Keep paragraphs where possible. Bounded overlapping windows for long paragraphs.
    start = 0
    position = 0
    while start < len(text):
        end = min(len(text), start + chunk_size)
        if end < len(text):
            boundary = text.rfind("\n", start + chunk_size // 2, end)
            if boundary > start:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            cur = con.execute("INSERT INTO chunks(page_id,position,text) VALUES (?,?,?)", (page_id, position, chunk))
            con.execute("INSERT INTO chunk_fts VALUES (?,?,?,?)", (terms(title), terms(chunk), cur.lastrowid, page_id))
            position += 1
        if end >= len(text):
            break
        start = max(start + 1, end - 160)
