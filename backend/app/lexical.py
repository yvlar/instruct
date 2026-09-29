"""Local, recoverable FTS5 sidecar; Qdrant manifests remain authoritative."""

import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from .indexing import IndexErrorBase, active_filter, digest

STOP_WORDS = set(
    "a au aux avec ce ces dans de des du en et est la le les l d un une pour quelle quel quelles quels que qui comment doit on il faut sur se son sa ses".split()
)
ATOM = re.compile(
    r"[a-z0-9]+(?:[-_/.][a-z0-9]+)+|[a-z]*\d+[a-z][a-z0-9]*|[+-]?\d+(?:[.,]\d+)?|[a-z][a-z0-9]*"
)
MEASURE = re.compile(
    r"(?<![\w.,])([+-]?\d+(?:[.,]\d+)?)\s*(°\s*c|%|n[· .]?m|tr/min|rpm|bar|kpa|mpa|psi|mm|cm|ml|kg|hz|v|a|m|g|l)(?![a-z0-9])"
)


def normalize(text: str) -> str:
    text = text.casefold().translate(str.maketrans("’‘‐‑–−", "''----"))
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def encoded(value: str) -> str:
    # Opaque single FTS tokens avoid losing decimal/code punctuation to unicode61.
    return "exact" + value.encode().hex()


def terms(text: str) -> tuple[list[str], list[str]]:
    clean = normalize(text)
    words, anchors = [], []
    for atom in ATOM.findall(clean):
        if any(c.isdigit() for c in atom):
            token = encoded(atom.replace(",", "."))
            words.append(token)
            anchors.append(token)
        elif atom not in STOP_WORDS:
            words.extend(p for p in re.split(r"[-_/.]", atom) if p not in STOP_WORDS)
    for value, unit in MEASURE.findall(clean):
        number = value.replace(",", ".")
        # Compact units (12bar) and spaced units (12 bar) expose the same anchors.
        anchors.extend([encoded(number), encoded(number + re.sub(r"[ .·]", "", unit))])
    if re.search(r"\barret\s+d[' ]urgence\b", clean):
        anchors.append(encoded("arret d'urgence"))
    return list(dict.fromkeys(words + anchors)), list(dict.fromkeys(anchors))


def exact_match(question: str, text: str) -> bool:
    _, wanted = terms(question)
    tokens, _ = terms(text)
    return bool(wanted) and set(wanted).issubset(tokens)


class LexicalIndex:
    def __init__(self, settings):
        self.path = Path(settings.lexical_index_path).resolve() / (
            digest([settings.qdrant_url, settings.qdrant_collection]) + ".sqlite3"
        )

    @contextmanager
    def connect(self, *, create=False):
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.exists():
            raise IndexErrorBase(
                "LEXICAL_INDEX_INCOMPLETE: relancez /api/ingest pour reconstruire SQLite depuis Qdrant."
            )
        db = sqlite3.connect(
            self.path if create else f"{self.path.as_uri()}?mode=rw", uri=not create
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA cache_size=-4096")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1) or (not create and version == 0):
                raise IndexErrorBase(
                    "LEXICAL_INDEX_INCOMPATIBLE: reconstruisez les index dans une nouvelle collection."
                )
            if create and version == 0:
                db.executescript("""
                    CREATE TABLE passages (
                        id TEXT PRIMARY KEY, document TEXT NOT NULL,
                        revision TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        page INTEGER NOT NULL, text TEXT NOT NULL, tokens TEXT NOT NULL
                    );
                    CREATE INDEX revisions ON passages(revision);
                    CREATE VIRTUAL TABLE fts USING fts5(tokens, content=passages, content_rowid=rowid);
                    CREATE TRIGGER passage_insert AFTER INSERT ON passages BEGIN
                        INSERT INTO fts(rowid,tokens) VALUES (new.rowid,new.tokens);
                    END;
                    CREATE TRIGGER passage_delete AFTER DELETE ON passages BEGIN
                        INSERT INTO fts(fts,rowid,tokens) VALUES ('delete',old.rowid,old.tokens);
                    END;
                    CREATE TRIGGER passage_update AFTER UPDATE ON passages BEGIN
                        INSERT INTO fts(fts,rowid,tokens) VALUES ('delete',old.rowid,old.tokens);
                        INSERT INTO fts(rowid,tokens) VALUES (new.rowid,new.tokens);
                    END;
                    PRAGMA user_version=1;
                """)
            yield db
            db.commit()
        finally:
            db.close()

    def upsert(self, points):
        with self.connect(create=True) as db:
            db.executemany(
                """INSERT INTO passages(id,document,revision,fingerprint,page,text,tokens)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                document=excluded.document,revision=excluded.revision,
                fingerprint=excluded.fingerprint,page=excluded.page,text=excluded.text,tokens=excluded.tokens""",
                [
                    (
                        str(p.id),
                        p.payload["document"],
                        p.payload["revision"],
                        p.payload["fingerprint"],
                        p.payload["page"],
                        p.payload["text"],
                        " ".join(terms(p.payload["text"])[0]),
                    )
                    for p in points
                ],
            )

    def missing(self, manifests, *, create=False):
        with self.connect(create=create) as db:
            counts = dict(
                db.execute("SELECT revision,count(*) FROM passages GROUP BY revision")
            )
        return {
            doc: m
            for doc, m in manifests.items()
            if counts.get(m["revision"], 0) != m["chunks"]
        }

    def repair(self, qdrant, collection, manifests):
        # Recovery also covers loss of the sidecar; never re-embed unchanged PDFs.
        for document, manifest in self.missing(manifests, create=True).items():
            with self.connect() as db:
                db.execute(
                    "DELETE FROM passages WHERE revision=?", (manifest["revision"],)
                )
            offset = None
            while True:
                points, offset = qdrant.scroll(
                    collection,
                    scroll_filter=active_filter({document: manifest}),
                    limit=128,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                self.upsert(points)
                if offset is None:
                    break
        self.require_ready(manifests)

    def require_ready(self, manifests):
        if self.missing(manifests):
            raise IndexErrorBase(
                "LEXICAL_INDEX_INCOMPLETE: relancez /api/ingest; une revision active manque dans SQLite."
            )

    @staticmethod
    def active_table(db, manifests):
        db.execute(
            "CREATE TEMP TABLE active(revision TEXT PRIMARY KEY, document TEXT NOT NULL)"
        )
        db.executemany(
            "INSERT INTO active VALUES (?,?)",
            [(m["revision"], doc) for doc, m in manifests.items()],
        )

    def collect_garbage(self, manifests):
        with self.connect(create=True) as db:
            self.active_table(db, manifests)
            db.execute(
                "DELETE FROM passages WHERE NOT EXISTS (SELECT 1 FROM active a WHERE a.revision=passages.revision AND a.document=passages.document)"
            )

    def search(self, question, manifests, limit):
        tokens, anchors = terms(question)
        if not tokens or not manifests:
            return []
        with self.connect() as db:
            self.active_table(db, manifests)
            # Reserve candidates containing ALL exact anchors before a broad OR query.
            # Filter revisions before LIMIT, so obsolete rows cannot crowd out active hits.
            queries = [" AND ".join(f'"{t}"' for t in anchors)] if anchors else []
            queries.append(" OR ".join(f'"{t}"' for t in tokens))
            found = {}
            for query in queries:
                rows = db.execute(
                    """SELECT p.*, bm25(fts) AS rank FROM fts
                    JOIN passages p ON p.rowid=fts.rowid
                    JOIN active a ON a.revision=p.revision AND a.document=p.document
                    WHERE fts MATCH ? ORDER BY rank,p.id LIMIT ?""",
                    (query, limit),
                ).fetchall()
                for row in rows:
                    found.setdefault(row["id"], dict(row))
                if len(found) >= limit:
                    break
        return list(found.values())[:limit]
