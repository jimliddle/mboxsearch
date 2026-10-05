"""Persistent, read-only mbox access. Only the cache database is modified."""

import email
from email.header import decode_header
from email.parser import HeaderParser
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from dataclasses import dataclass


FIELDS = ("all", "subject", "from", "to", "content")
SCHEMA_VERSION = 1


class MailboxChangedError(RuntimeError):
    pass


def parse_terms(terms, field="all"):
    """Retain the original single-term --field and AND search semantics."""
    if field not in FIELDS:
        raise ValueError("Unknown search field: " + field)
    if len(terms) == 1 and field != "all":
        return [(terms[0], field)]
    result = []
    for term in terms:
        prefix, separator, value = term.partition(":")
        if separator:
            if prefix not in FIELDS:
                raise ValueError("Unknown search field: " + prefix)
            result.append((value, prefix))
        else:
            result.append((term, "all"))
    return result


def compile_term(term, exact):
    pattern = re.escape(term)
    if exact:
        pattern = r"\b" + pattern + r"\b"
    return re.compile(pattern, re.IGNORECASE)


def legacy_text(raw):
    # The original script used a UTF-8 text stream with universal newlines.
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


def binary_lines(stream):
    """Universal newlines with original byte offsets and bounded read buffers."""
    fragments = []
    held_cr = b""
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        chunk = held_cr + chunk
        held_cr = b""
        if chunk.endswith(b"\r"):
            chunk, held_cr = chunk[:-1], b"\r"
        start = 0
        for ending in re.finditer(b"\r\n|\r|\n", chunk):
            end = ending.end()
            if fragments:
                fragments.append(chunk[start:end])
                yield b"".join(fragments)
                fragments = []
            else:
                yield chunk[start:end]
            start = end
        if start < len(chunk):
            fragments.append(chunk[start:])
    if held_cr:
        fragments.append(held_cr)
    if fragments:
        yield b"".join(fragments)


def display_header(value):
    if not value:
        return ""
    pieces = []
    for text, charset in decode_header(value):
        if isinstance(text, bytes):
            try:
                text = text.decode(charset or "ascii", errors="replace")
            except LookupError:
                text = text.decode("utf-8", errors="replace")
        pieces.append(text)
    return "".join(pieces)


def discover_mboxes(target):
    path = Path(target).expanduser().resolve()
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise FileNotFoundError("Mailbox path does not exist: " + str(path))
    return sorted({item.resolve() for item in path.rglob("*.mbox") if item.is_file()})


def signature(stat):
    return json.dumps([stat.st_dev, stat.st_ino, stat.st_size,
                       stat.st_mtime_ns, stat.st_ctime_ns])


def default_cache_dir():
    root = os.environ.get("XDG_CACHE_HOME")
    if not root and os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
    return Path(root or Path.home() / ".cache") / "mboxsearch"


@dataclass(frozen=True)
class MessageInfo:
    id: int
    file: str
    index: int
    subject: str
    sender: str
    recipient: str
    date: str


class Results:
    """Disk-backed result IDs; paging does not load all headers or bodies."""

    def __init__(self, store, table):
        self.store = store
        self.table = table
        self.count = store.db.execute("SELECT count(*) FROM " + table).fetchone()[0]

    def __len__(self):
        return self.count

    def page(self, start, limit):
        if start < 0 or limit < 0:
            raise ValueError("Page bounds must be nonnegative")
        rows = self.store.db.execute(
            "SELECT m.id, s.path, m.number, m.subject, m.sender, m.recipient, m.date "
            "FROM " + self.table + " r JOIN messages m ON m.id=r.message_id "
            "JOIN sources s ON s.id=m.source_id WHERE r.position>=? "
            "ORDER BY r.position LIMIT ?", (start + 1, limit))
        return [MessageInfo(*row) for row in rows]

    def __getitem__(self, position):
        if position < 0:
            position += self.count
        if not 0 <= position < self.count:
            raise IndexError(position)
        page = self.page(position, 1)
        if not page:
            raise MailboxChangedError("Mailbox refreshed; run the search again")
        return page[0]

    def __iter__(self):
        for start in range(0, self.count, 100):
            yield from self.page(start, 100)

    def close(self):
        self.store.db.execute("DROP TABLE IF EXISTS " + self.table)


class MailboxStore:
    def __init__(self, target, cache_dir=None, rebuild=False, progress=None):
        self.target = Path(target).expanduser().resolve()
        self.progress = progress or (lambda phase, path, done, total: None)
        cache = Path(cache_dir) if cache_dir is not None else default_cache_dir()
        cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = hashlib.sha256(os.fsencode(str(self.target))).hexdigest()
        self.cache_path = cache / (key + ".sqlite3")
        descriptor = os.open(str(self.cache_path), os.O_CREAT | os.O_RDWR, 0o600)
        os.close(descriptor)
        self.db = sqlite3.connect(str(self.cache_path), timeout=30)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute("PRAGMA cache_size=-8192")
        self._generation = 0
        self._patterns = {}
        self.db.create_function("matches", 2, self._matches)
        try:
            self._schema()
            self.refresh(rebuild)
        except BaseException:
            self.db.close()
            raise

    def _schema(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise RuntimeError("Unsupported cache version; choose a new --cache-dir")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                number INTEGER NOT NULL, start INTEGER NOT NULL, end INTEGER NOT NULL,
                subject_raw TEXT, sender_raw TEXT, recipient_raw TEXT,
                subject TEXT NOT NULL, sender TEXT NOT NULL,
                recipient TEXT NOT NULL, date TEXT NOT NULL,
                UNIQUE(source_id, number)
            );
            CREATE TABLE IF NOT EXISTS search_text (
                message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                text TEXT NOT NULL
            );
            PRAGMA user_version=1;
        """)

    def _matches(self, key, text):
        return int(text is not None and bool(self._patterns[key].search(text)))

    def refresh(self, rebuild=False):
        files = discover_mboxes(self.target)
        paths = {str(path) for path in files}
        with self.db:
            for source_id, path in self.db.execute("SELECT id,path FROM sources").fetchall():
                if path not in paths:
                    self.db.execute("DELETE FROM sources WHERE id=?", (source_id,))
        for path in files:
            current = signature(path.stat())
            cached = self.db.execute("SELECT signature FROM sources WHERE path=?", (str(path),)).fetchone()
            if rebuild or cached is None or cached[0] != current:
                self._index_file(path)
        self.files = files

    def _index_file(self, path):
        before = signature(path.stat())
        with path.open("rb") as stream, self.db:
            stream_before = signature(os.fstat(stream.fileno()))
            self._verify_stream(path, stream, before, stream_before)
            total = os.fstat(stream.fileno()).st_size
            self.db.execute("DELETE FROM sources WHERE path=?", (str(path),))
            source_id = self.db.execute("INSERT INTO sources(path,signature) VALUES (?,?)",
                                        (str(path), before)).lastrowid
            self.progress("Indexing", path, 0, total)
            start = position = number = 0
            headers = []
            in_headers = True
            next_report = 8 * 1024 * 1024
            for line in binary_lines(stream):
                if line.startswith(b"From ") and position > start:
                    self._insert_header(source_id, number, start, position, headers)
                    number += 1
                    start = position
                    headers = []
                    in_headers = True
                if in_headers:
                    headers.append(line)
                    if line in (b"\n", b"\r\n", b"\r"):
                        in_headers = False
                position += len(line)
                if position >= next_report:
                    self.progress("Indexing", path, position, total)
                    next_report = position + 8 * 1024 * 1024
            if position > start:
                self._insert_header(source_id, number, start, position, headers)
            self._verify_stream(path, stream, before, stream_before)
            self.progress("Indexing", path, total, total)

    def _insert_header(self, source_id, number, start, end, headers):
        message = HeaderParser().parsestr(legacy_text(b"".join(headers)))
        values = [message.get(name) for name in ("subject", "from", "to")]
        self.db.execute(
            "INSERT INTO messages(source_id,number,start,end,subject_raw,sender_raw,"
            "recipient_raw,subject,sender,recipient,date) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, number, start, end, *values,
             *(display_header(value) for value in values), display_header(message.get("date"))))

    @staticmethod
    def _verify_stream(path, stream, expected, stream_expected):
        # Compare each API with its own snapshot. Windows stat/fstat can report
        # different timestamps for the same unchanged file. File identity must
        # still agree, so a replacement between stat() and open() is rejected.
        current_stream = signature(os.fstat(stream.fileno()))
        if (current_stream != stream_expected or signature(path.stat()) != expected
                or json.loads(current_stream)[:2] != json.loads(expected)[:2]):
            raise MailboxChangedError("Mailbox changed while reading: " + str(path) + "; refresh or reopen it")

    def _verify_sources(self):
        for path, expected in self.db.execute("SELECT path,signature FROM sources"):
            try:
                current = signature(Path(path).stat())
            except FileNotFoundError:
                current = None
            if current != expected:
                raise MailboxChangedError("Mailbox changed: " + path + "; refresh or reopen it")

    def _ensure_search_text(self, source_id):
        row = self.db.execute("SELECT path,signature FROM sources WHERE id=?", (source_id,)).fetchone()
        path, expected = Path(row[0]), row[1]
        missing = self.db.execute(
            "SELECT count(*) FROM messages m LEFT JOIN search_text t ON t.message_id=m.id "
            "WHERE m.source_id=? AND t.message_id IS NULL", (source_id,)).fetchone()[0]
        if not missing:
            return
        # One transaction per source: a cancelled/failed build never marks it complete.
        with path.open("rb") as stream, self.db:
            stream_expected = signature(os.fstat(stream.fileno()))
            self._verify_stream(path, stream, expected, stream_expected)
            cursor = self.db.execute(
                "SELECT m.id,m.start,m.end FROM messages m LEFT JOIN search_text t ON t.message_id=m.id "
                "WHERE m.source_id=? AND t.message_id IS NULL ORDER BY m.number", (source_id,))
            self.progress("Caching search text", path, 0, missing)
            for done, (message_id, start, end) in enumerate(cursor, 1):
                stream.seek(start)
                message = email.message_from_string(legacy_text(stream.read(end - start)))
                self.db.execute("INSERT INTO search_text(message_id,text) VALUES (?,?)",
                                (message_id, message.as_string()))
                if done % 250 == 0:
                    self.progress("Caching search text", path, done, missing)
            self._verify_stream(path, stream, expected, stream_expected)
            self.progress("Caching search text", path, missing, missing)

    def search(self, terms=(), exact=False):
        self._verify_sources()
        if any(field not in FIELDS for _, field in terms):
            raise ValueError("Unknown search field")
        body = any(field in ("all", "content") for _, field in terms)
        if body:
            for (source_id,) in self.db.execute("SELECT id FROM sources ORDER BY path").fetchall():
                self._ensure_search_text(source_id)
        self._generation += 1
        table = "results_" + str(self._generation)
        clauses, keys = [], []
        columns = {"subject": "m.subject_raw", "from": "m.sender_raw", "to": "m.recipient_raw",
                   "all": "t.text", "content": "t.text"}
        for term, field in terms:
            key = str(len(self._patterns))
            self._patterns[key] = compile_term(term, exact)
            clauses.append("matches(?," + columns[field] + ")")
            keys.append(key)
        try:
            with self.db:
                self.db.execute("CREATE TEMP TABLE " + table +
                                " (position INTEGER PRIMARY KEY, message_id INTEGER NOT NULL)")
                query = "SELECT m.id FROM messages m JOIN sources s ON s.id=m.source_id "
                if body:
                    query += "JOIN search_text t ON t.message_id=m.id "
                if clauses:
                    query += "WHERE " + " AND ".join(clauses) + " "
                query += "ORDER BY s.path,m.number"
                self.db.execute("INSERT INTO " + table + "(message_id) " + query, keys)
                self._verify_sources()
        finally:
            for key in keys:
                self._patterns.pop(key, None)
        return Results(self, table)

    def _read_message_bytes(self, info):
        row = self.db.execute(
            "SELECT s.path,s.signature,m.start,m.end FROM messages m "
            "JOIN sources s ON s.id=m.source_id WHERE m.id=?", (info.id,)).fetchone()
        if row is None or row[0] != info.file:
            raise MailboxChangedError("Mailbox refreshed; run the search again")
        path, expected, start, end = row
        with open(path, "rb") as stream:
            stream_expected = signature(os.fstat(stream.fileno()))
            self._verify_stream(Path(path), stream, expected, stream_expected)
            stream.seek(start)
            raw = stream.read(end - start)
            self._verify_stream(Path(path), stream, expected, stream_expected)
        return raw

    def get_message(self, info):
        """Retain the original text-parser behavior for search/API compatibility."""
        return email.message_from_string(legacy_text(self._read_message_bytes(info)))

    def get_mime_message(self, info):
        """Preserve original payload bytes for reading and attachment extraction."""
        return email.message_from_bytes(self._read_message_bytes(info))

    def at_index(self, index):
        # --view historically means this zero-based index in the first file that has it.
        row = self.db.execute(
            "SELECT m.id,s.path,m.number,m.subject,m.sender,m.recipient,m.date "
            "FROM messages m JOIN sources s ON s.id=m.source_id "
            "WHERE m.number=? ORDER BY s.path LIMIT 1", (index,)).fetchone()
        return MessageInfo(*row) if row else None

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
