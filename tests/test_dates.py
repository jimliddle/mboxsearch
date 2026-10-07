import email
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mbox_index import MailboxStore, parse_date_bound, parse_terms
from mbox_search import check_term, main


MESSAGES = [
    ("John Smith", "Wed, 31 Jan 2024 23:30:00 -0200", "Invoice", "payment details"),
    ("John Smith", "Thu, 01 Feb 2024 00:00:00 +0000", "Invoice", "payment details"),
    ("John Smith", "Thu, 29 Feb 2024 23:30:00 -0200", "Invoice", "payment details"),
    ("John Smith", "Thu, 01 Feb 2024 00:30:00 +0200", "Invoice", "payment details"),
    ("John Smith", "Thu, 15 Feb 2024 12:00:00", "Meeting", "different body"),
    ("Jane Smith", "Thu, 15 Feb 2024 12:00:00 +0000", "Invoice", "payment details"),
    ("John Smith", None, "Invoice", "payment details"),
    ("John Smith", "not a message date", "Invoice", "payment details"),
]


class DateSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mail = self.root / "dates.mbox"
        self.cache = self.root / "cache"
        data = []
        for sender, sent, subject, body in MESSAGES:
            headers = ["From sender", "From: " + sender, "Subject: " + subject]
            if sent is not None:
                headers.append("Date: " + sent)
            data.append("\n".join(headers) + "\n\n" + body + "\n\n")
        self.mail.write_text("".join(data), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def indices(self, store, tokens, exact=False):
        results = store.search(parse_terms(tokens), exact)
        try:
            return [info.index for info in results]
        finally:
            results.close()

    def test_sender_range_utc_boundaries_and_missing_dates(self):
        with MailboxStore(self.mail, self.cache) as store:
            self.assertEqual(self.indices(store, ["from:John Smith", "after:2024-02-01",
                                                   "before:2024-03-01"]), [0, 1, 4])
            self.assertEqual(self.indices(store, ["after:2024-02-01", "before:2024-03-01"]),
                             [0, 1, 4, 5])
            self.assertEqual(self.indices(store, ["from:John Smith"]), [0, 1, 2, 3, 4, 6, 7])
            self.assertEqual(store.db.execute("SELECT count(*) FROM search_text").fetchone()[0], 0)

    def test_open_ended_bounds_duplicate_bounds_and_empty_range(self):
        with MailboxStore(self.mail, self.cache) as store:
            self.assertEqual(self.indices(store, ["before:2024-02-01"]), [3])
            self.assertEqual(self.indices(store, ["after:2024-03-01"]), [2])
            self.assertEqual(self.indices(store, ["after:2024-01-01", "after:2024-02-01",
                                                   "before:2024-04-01", "before:2024-03-01"]),
                             [0, 1, 4, 5])
            self.assertEqual(self.indices(store, ["after:2024-02-01", "before:2024-02-01"]), [])

    def test_name_keyword_and_range_keep_and_and_exact_semantics(self):
        with MailboxStore(self.mail, self.cache) as store:
            tokens = ["from:John Smith", "after:2024-02-01", "before:2024-03-01",
                      "subject:Invoice", "content:payment"]
            self.assertEqual(self.indices(store, tokens), [0, 1])
            self.assertEqual(self.indices(store, tokens, exact=True), [0, 1])
            tokens[-1] = "content:pay"
            self.assertEqual(self.indices(store, tokens, exact=True), [])
            self.assertEqual(store._patterns, {})

    def test_update_reuses_version_one_offsets_and_full_content_cache(self):
        # Build a version-1 database, then reopen with source parsing disabled.
        with MailboxStore(self.mail, self.cache) as store:
            store.search([("payment", "content")]).close()
            cache_path = store.cache_path
            offsets = store.db.execute("SELECT id,start,end FROM messages").fetchall()
            text = store.db.execute("SELECT message_id,text FROM search_text").fetchall()
            schema = store.db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall()
        with patch.object(MailboxStore, "_index_file", side_effect=AssertionError("reindexed")), \
                patch("mbox_index.HeaderParser.parsestr", side_effect=AssertionError("header parsed")), \
                patch("mbox_index.email.message_from_string", side_effect=AssertionError("body parsed")), \
                patch("pathlib.Path.open", side_effect=AssertionError("mbox opened")):
            with MailboxStore(self.mail, self.cache) as store:
                self.assertEqual(store.cache_path, cache_path)
                self.assertEqual(self.indices(store, ["from:John Smith", "after:2024-02-01",
                                                       "before:2024-03-01", "payment"]), [0, 1])
                self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 1)
                self.assertEqual(store.db.execute("SELECT id,start,end FROM messages").fetchall(), offsets)
                self.assertEqual(store.db.execute("SELECT message_id,text FROM search_text").fetchall(), text)
                self.assertEqual(store.db.execute("SELECT name,sql FROM sqlite_master ORDER BY name").fetchall(), schema)

    def test_header_date_query_never_constructs_or_reads_body_cache(self):
        with MailboxStore(self.mail, self.cache) as store:
            with patch.object(store, "_ensure_search_text", side_effect=AssertionError("body cache used")), \
                    patch("mbox_index.email.message_from_string", side_effect=AssertionError("body parsed")):
                self.assertEqual(self.indices(store, ["subject:Invoice", "after:2024-02-01",
                                                       "before:2024-03-01"]), [0, 1, 5])

    def test_invalid_dates_rejected_before_content_cache_work(self):
        invalid = ["", "2024-2-01", "20240201", "2023-02-29", "2024-13-01",
                   "2024-01-32", "2024-01-01T12:00:00", "0000-01-01"]
        with MailboxStore(self.mail, self.cache) as store:
            for value in invalid:
                with self.subTest(value=value):
                    with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                        parse_terms(["after:" + value])
                    with self.assertRaisesRegex(ValueError, "YYYY-MM-DD"):
                        store.search([("payment", "content"), (value, "before")])
            self.assertEqual(store.db.execute("SELECT count(*) FROM search_text").fetchone()[0], 0)
        self.assertIsInstance(parse_date_bound("2024-02-29"), int)

    def test_legacy_helper_supports_date_filters_and_pre_epoch_dates(self):
        for sender, sent, subject, body in MESSAGES:
            message = email.message_from_string("Date: " + (sent or "") + "\n\n" + body)
            expected = sent in (MESSAGES[0][1], MESSAGES[1][1], MESSAGES[4][1], MESSAGES[5][1])
            self.assertEqual(check_term(message, "2024-02-01", "after", True) and
                             check_term(message, "2024-03-01", "before", True), expected)
        message = email.message_from_string("Date: Wed, 01 Jan 1969 12:00:00 +0000\n\nBody")
        self.assertTrue(check_term(message, "1969-01-01", "after", False))
        self.assertTrue(check_term(message, "1970-01-01", "before", False))

    def test_cli_combines_sender_and_date_filters(self):
        output = io.StringIO()
        with patch("sys.stdout", output):
            result = main([str(self.mail), "from:John Smith", "after:2024-02-01",
                           "before:2024-03-01", "--cache-dir", str(self.cache), "--list"])
        self.assertEqual(result, 0)
        self.assertIn("Found 3 matching emails", output.getvalue())
        with patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                main([str(self.mail), "after:2023-02-29", "--list"])
        self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
