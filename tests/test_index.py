import email
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mbox_index import MailboxChangedError, MailboxStore, binary_lines, parse_terms, signature
from mbox_search import check_term, main, search_mbox


SAMPLE = (
    b"From alice@example.com Sat Jan 01 00:00:00 2022\n"
    b"From: Alice <alice@example.com>\nTo: Bob <bob@example.com>\n"
    b"Subject: License renewal\n\tfor project K\nDate: 1 Jan 2022\n\n"
    b"Important license details and licensing.\n>From escaped body line\n\n"
    b"From bob@example.com Sun Jan 02 00:00:00 2022\n"
    b"From: Bob <bob@example.com>\nTo: alice@example.com\n"
    b"Subject: =?utf-8?b?Q2Fmw6k=?=\nMIME-Version: 1.0\n"
    b"Content-Type: multipart/mixed; boundary=bound\n\n"
    b"--bound\nContent-Type: text/plain; charset=iso-8859-1\n"
    b"Content-Transfer-Encoding: quoted-printable\n\nCaf=E9 body\n"
    b"--bound\nContent-Type: application/octet-stream\n"
    b"Content-Disposition: attachment; filename=test.bin\n"
    b"Content-Transfer-Encoding: base64\n\nYWJj\n--bound--\n\n"
    b"From nobody Mon Jan 03 00:00:00 2022\nX-Extra: headeronly\n\n"
    b"No subject; 100% literal _ [regex] and bad byte \xff"
)


def reference_messages(raw):
    """The old script's delimiter/parser contract, independent of the new index."""
    text = raw.decode("utf-8", "replace").replace("\r\n", "\n").replace("\r", "\n")
    buffer, messages = [], []
    for line in io.StringIO(text):
        if line.startswith("From ") and buffer:
            messages.append(email.message_from_string("".join(buffer)))
            buffer = []
        buffer.append(line)
    if buffer:
        messages.append(email.message_from_string("".join(buffer)))
    return messages


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.mail = self.root / "mail.mbox"
        self.mail.write_bytes(SAMPLE)
        self.cache = self.root / "cache"

    def tearDown(self):
        self.temp.cleanup()

    def store(self, **kwargs):
        return MailboxStore(self.mail, self.cache, **kwargs)

    def test_search_compatibility_and_lazy_offsets(self):
        queries = [[], [("license", "subject")], [("license", "content")],
                   [("alice", "from"), ("bob", "to")], [("headeronly", "content")],
                   [("Caf=E9", "all")], [("Café", "subject")], [("100%", "all")],
                   [("[regex]", "content")], [("_", "all")], [("", "subject")],
                   [("K", "subject")], [("renewal\n", "subject")]]
        for raw in (SAMPLE, SAMPLE.replace(b"\n", b"\r\n"), SAMPLE.replace(b"\n", b"\r")):
            self.mail.write_bytes(raw)
            reference = reference_messages(raw)
            with self.store() as store:
                for exact in (False, True):
                    for query in queries:
                        with self.subTest(exact=exact, query=query, crlf=raw != SAMPLE):
                            expected = [i for i, message in enumerate(reference)
                                        if all(check_term(message, term, field, exact) for term, field in query)]
                            results = store.search(query, exact)
                            self.assertEqual([info.index for info in results], expected)
                            for info in results:
                                self.assertEqual(store.get_message(info).as_string(), reference[info.index].as_string())
                            results.close()
                self.assertEqual(store.search()[1].subject, "Café")
                self.assertEqual(store.at_index(2).index, 2)
                self.assertIsNone(store.at_index(3))

    def test_warm_browse_and_header_search_never_parse_messages(self):
        with self.store() as store:
            self.assertEqual(store.db.execute("SELECT count(*) FROM search_text").fetchone()[0], 0)
        with patch("mbox_index.email.message_from_string", side_effect=AssertionError("body parsed")), \
                patch("mbox_index.HeaderParser.parsestr", side_effect=AssertionError("header reparsed")):
            with self.store() as store:
                self.assertEqual(len(store.search()), 3)
                results = store.search([("License", "subject")])
                self.assertEqual(len(results), 1)
                self.assertEqual(results.page(0, 1)[0].index, 0)
                self.assertEqual(store.db.execute("SELECT count(*) FROM search_text").fetchone()[0], 0)

    def test_content_cache_reused_and_one_selected_message_loaded(self):
        original = email.message_from_string
        with patch("mbox_index.email.message_from_string", wraps=original) as parse:
            with self.store() as store:
                self.assertEqual(len(store.search([("license", "all")])), 1)
                self.assertEqual(parse.call_count, 3)
                self.assertEqual(len(store.search([("Caf=E9", "content")])), 1)
                self.assertEqual(parse.call_count, 3)
        with patch("mbox_index.email.message_from_string", wraps=original) as parse:
            with self.store() as store:
                results = store.search([("headeronly", "all")])
                self.assertEqual(parse.call_count, 0)
                store.get_message(results[0])
                self.assertEqual(parse.call_count, 1)

    def test_invalidation_append_truncate_and_same_size_edit(self):
        with self.store() as store:
            store.search([("license", "all")])
            info = store.search()[0]
            self.mail.write_bytes(SAMPLE + b"\nFrom appended\nSubject: New\n\nBody\n")
            with self.assertRaises(MailboxChangedError):
                store.get_message(info)
            with self.assertRaises(MailboxChangedError):
                store.search()
            store.refresh()
            self.assertEqual(len(store.search()), 4)
            self.assertEqual(store.db.execute("SELECT count(*) FROM search_text").fetchone()[0], 0)
        self.mail.write_bytes(SAMPLE)
        with self.store() as store:
            old_stat = self.mail.stat()
            self.mail.write_bytes(SAMPLE.replace(b"License", b"Changed"))
            if os.name != "nt":
                # POSIX ctime detects edits even if mtime has been restored.
                os.utime(self.mail, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
            store.refresh()
            self.assertEqual(len(store.search([("Changed", "subject")])), 1)
            self.mail.write_bytes(b"")
            store.refresh()
            self.assertEqual(len(store.search()), 0)

    def test_file_replacement_invalidates_offsets(self):
        with self.store() as store:
            info = store.search()[0]
            replacement = self.root / "replacement"
            replacement.write_bytes(SAMPLE)
            os.replace(replacement, self.mail)
            with self.assertRaises(MailboxChangedError):
                store.get_message(info)
        with self.store() as store:
            self.assertEqual(len(store.search()), 3)

    def test_old_message_reference_rejected_after_refresh(self):
        with self.store() as store:
            results = store.search()
            old = results[0]
            self.mail.write_bytes(SAMPLE.replace(b"License", b"Changed"))
            store.refresh()
            with self.assertRaises(MailboxChangedError):
                store.get_message(old)
            with self.assertRaises(MailboxChangedError):
                results[0]

    def test_stat_and_fstat_timestamps_are_validated_independently(self):
        stat = self.mail.stat()
        handle_stat = SimpleNamespace(st_dev=stat.st_dev, st_ino=stat.st_ino,
                                      st_size=stat.st_size, st_mtime_ns=stat.st_mtime_ns,
                                      st_ctime_ns=stat.st_ctime_ns + 100)
        with self.mail.open("rb") as stream, patch("mbox_index.os.fstat", return_value=handle_stat):
            MailboxStore._verify_stream(self.mail, stream, signature(stat), signature(handle_stat))
            expected = signature(handle_stat)
            handle_stat.st_mtime_ns += 100
            with self.assertRaises(MailboxChangedError):
                MailboxStore._verify_stream(self.mail, stream, signature(stat), expected)
            handle_stat.st_ino += 1
            with self.assertRaises(MailboxChangedError):
                MailboxStore._verify_stream(self.mail, stream, signature(stat), signature(handle_stat))

    def test_universal_newlines_at_chunk_boundaries(self):
        for raw in (b"a" * (1024 * 1024 - 1) + b"\r\nLast\r",
                    b"a" * (1024 * 1024 + 20) + b"\nLast",
                    b"\r\n\r\n\n\r", b"\r"):
            self.assertEqual(list(binary_lines(io.BytesIO(raw))), raw.splitlines(keepends=True))

    def test_interrupted_rebuild_rolls_back(self):
        with self.store() as store:
            store.search([("license", "all")])
        def cancel(phase, path, done, total):
            if phase == "Indexing" and done == total:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.store(rebuild=True, progress=cancel)
        with patch("mbox_index.HeaderParser.parsestr", side_effect=AssertionError("reindexed")):
            with self.store() as store:
                self.assertEqual(len(store.search()), 3)
                self.assertEqual(store.db.execute("SELECT count(*) FROM search_text").fetchone()[0], 3)

    def test_interrupted_search_cache_rolls_back(self):
        def cancel(phase, path, done, total):
            if phase == "Caching search text" and done == total:
                raise KeyboardInterrupt()
        with self.store(progress=cancel) as store:
            with self.assertRaises(KeyboardInterrupt):
                store.search([("license", "all")])
            self.assertEqual(store.db.execute("SELECT count(*) FROM search_text").fetchone()[0], 0)
            store.progress = lambda *args: None
            self.assertEqual(len(store.search([("license", "all")])), 1)

    def test_change_during_index_not_committed(self):
        def modify(phase, path, done, total):
            if phase == "Indexing" and done == 0:
                with self.mail.open("ab") as stream:
                    stream.write(b"\nchanged")
        with self.assertRaises(MailboxChangedError):
            self.store(progress=modify)
        with self.store() as store:
            self.assertEqual(len(store.search()), 3)

    def test_recursive_discovery_removal_and_view_contract(self):
        folder = self.root / "archive"
        folder.mkdir()
        first = folder / "a.mbox"
        first.write_bytes(b"From one\nSubject: First\n\nbody\n")
        nested = folder / "nested"
        nested.mkdir()
        second = nested / "b.mbox"
        second.write_bytes(SAMPLE)
        with MailboxStore(folder, self.cache) as store:
            self.assertEqual(len(store.search()), 4)
            self.assertEqual(store.at_index(0).subject, "First")
            self.assertEqual(store.at_index(1).subject, "Café")
            first.unlink()
            store.refresh()
            self.assertEqual(len(store.search()), 3)

    def test_no_envelope_preamble_and_empty_files(self):
        samples = [b"", b"Subject: Single\n\nbody", b"preamble\n" + SAMPLE,
                   b"From one\nFrom two\nSubject: Last\n\nbody\n"]
        for raw in samples:
            self.mail.write_bytes(raw)
            with self.store() as store:
                self.assertEqual(len(store.search()), len(reference_messages(raw)))

    def test_large_results_paged_without_message_parsing(self):
        self.mail.write_bytes(b"From sender\nSubject: Test\n\nbody\n" * 5000)
        with self.store() as store:
            with patch("mbox_index.email.message_from_string", side_effect=AssertionError("parsed")):
                results = store.search()
                self.assertEqual(len(results), 5000)
                self.assertEqual([info.index for info in results.page(4990, 20)], list(range(4990, 5000)))
                self.assertEqual(results[-1].index, 4999)
                self.assertEqual(len(results.page(6000, 10)), 0)

    def test_cache_permissions_and_source_unchanged(self):
        before = self.mail.read_bytes()
        with self.store() as store:
            store.search([("license", "all")])
            if os.name != "nt":
                self.assertEqual(store.cache_path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.cache.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.mail.read_bytes(), before)

    def test_legacy_api(self):
        with patch("mbox_index.default_cache_dir", return_value=self.cache):
            results = search_mbox(str(self.mail), [("license", "subject")], False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][:2], (str(self.mail), 0))
        self.assertEqual(results[0][2]["Subject"], "License renewal\n\tfor project K")

    def test_cli_list_view_log_and_errors(self):
        log = self.root / "results.log"
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            code = main([str(self.mail), "license", "--field", "subject", "--list",
                         "--log", str(log), "--cache-dir", str(self.cache)])
            self.assertEqual(code, 0)
            self.assertIn("Found 1 matching emails", output.getvalue())
        self.assertIn("License renewal", log.read_text())
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main([str(self.mail), "--view", "1", "--cache-dir", str(self.cache)]), 0)
            self.assertIn("Café body", output.getvalue())
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main([str(self.root / "missing"), "--list", "--cache-dir", str(self.cache)]), 1)
            with self.assertRaises(SystemExit):
                main([str(self.mail), "invalid:term", "--list"])
            with self.assertRaises(SystemExit):
                main([str(self.mail), "--tui"])

    def test_parse_terms_and_literal_exact_matching(self):
        self.assertEqual(parse_terms(["term"], "subject"), [("term", "subject")])
        self.assertEqual(parse_terms(["from:alice", "term"]), [("alice", "from"), ("term", "all")])
        with self.assertRaises(ValueError):
            parse_terms(["oops:term"])
        message = email.message_from_string("Subject: license licensing\n\nbody")
        self.assertTrue(check_term(message, "license", "subject", True))
        self.assertFalse(check_term(message, "licens", "subject", True))


if __name__ == "__main__":
    unittest.main()
