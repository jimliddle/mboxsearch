import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import email

from mbox_index import MailboxStore
from mbox_render import message_headers, message_text, safe_text


class RenderTests(unittest.TestCase):
    def test_charset_html_attachments_and_control_characters(self):
        message = email.message_from_string(
            "Subject: =?utf-8?b?Q2Fmw6k=?=\nContent-Type: multipart/mixed; boundary=x\n\n"
            "--x\nContent-Type: text/plain; charset=iso-8859-1\n"
            "Content-Transfer-Encoding: quoted-printable\n\nCaf=E9\n"
            "--x\nContent-Type: text/plain\nContent-Disposition: attachment\n\nSECRET\n--x--")
        self.assertIn("Café", message_text(message))
        self.assertNotIn("SECRET", message_text(message))
        self.assertIn("Subject: Café", message_headers(message))
        html = email.message_from_string("Content-Type: text/html\n\n<p>Hello &amp; welcome</p><script>hidden</script>")
        self.assertIn("Hello & welcome", message_text(html))
        self.assertNotIn("hidden", message_text(html))
        self.assertNotIn("\x1b", safe_text("\x1b[31mSubject\nline"))
        self.assertNotIn("\n", safe_text("Subject\nline"))
        self.assertIn("truncated", message_text(html, limit=4))

    def test_unknown_charset_and_plain_alternative(self):
        message = email.message_from_string("Content-Type: text/plain; charset=unknown-xyz\n\nReadable")
        self.assertEqual(message_text(message), "Readable")
        message = email.message_from_string(
            "Content-Type: multipart/alternative; boundary=x\n\n--x\nContent-Type: text/plain\n\nPlain\n"
            "--x\nContent-Type: text/html\n\n<p>HTML</p>\n--x--")
        self.assertIn("Plain", message_text(message))
        self.assertNotIn("HTML", message_text(message))


@unittest.skipUnless(importlib.util.find_spec("curses"), "curses unavailable")
class BrowserTests(unittest.TestCase):
    def setUp(self):
        from mbox_tui import Browser
        self.temp = tempfile.TemporaryDirectory()
        path = Path(self.temp.name)
        mail = path / "sample.mbox"
        mail.write_bytes(b"From alice\nSubject: One\nFrom: Alice\n\nFirst body\n"
                         b"From bob\nSubject: Two\nFrom: Bob\n\nSecond body\n")
        self.store = MailboxStore(mail, path / "cache")
        self.browser = Browser(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_navigation_lazy_read_search_and_exact(self):
        browser = self.browser
        with patch.object(self.store, "get_message", wraps=self.store.get_message) as load:
            browser.move(100)
            self.assertEqual(browser.selected, 1)
            self.assertIsNone(browser.body)
            load.assert_not_called()
            browser.open_selected()
            self.assertIn("Second body", browser.body)
            self.assertTrue(browser.focus_body)
            self.assertEqual(load.call_count, 1)
            browser.move(-1)
            self.assertIsNone(browser.body)
            browser.apply_search("subject:Two")
            self.assertEqual(len(browser.results), 1)
            self.assertEqual(browser.selected, 0)
            browser.apply_search("subject:Tw", exact=True)
            self.assertEqual(len(browser.results), 0)
            browser.move(1)
            browser.open_selected()
            self.assertEqual(browser.selected, 0)
            browser.apply_search("")
            self.assertEqual(len(browser.results), 2)
        with self.assertRaises(ValueError):
            browser.apply_search("invalid:term")
        self.assertEqual(len(browser.results), 2)

    def test_draw_resize_help_and_keyboard_loop(self):
        import curses
        class Screen:
            def __init__(self, keys):
                self.keys = iter(keys)
                self.lines = []
                self.size = (24, 80)
            def getmaxyx(self):
                return self.size
            def addnstr(self, row, col, text, length, attr):
                self.lines.append(text[:length])
            def refresh(self):
                pass
            def erase(self):
                pass
            def keypad(self, enabled):
                pass
            def get_wch(self):
                return next(self.keys)
        screen = Screen([curses.KEY_DOWN, "\n", "\t", "c", "?", "h", "q"])
        with patch("mbox_tui.curses.curs_set"):
            self.browser.run(screen)
        self.assertTrue(any("Subject: Two" in line for line in screen.lines))
        self.assertTrue(any("Fields:" in line for line in screen.lines))
        screen.size = (5, 20)
        self.browser.draw()
        self.assertTrue(any("Resize terminal" in line for line in screen.lines))

    def test_field_option_and_explicit_prefixes_in_ui(self):
        from mbox_tui import Browser
        browser = Browser(self.store, [("Two", "subject")], field="subject")
        browser.apply_search(browser.query, exact=True)
        self.assertEqual(len(browser.results), 1)
        browser.apply_search("from:Alice")
        self.assertEqual(len(browser.results), 1)
        self.assertEqual(browser.results[0].subject, "One")

    def test_refresh_and_failed_content_search_leave_usable_list(self):
        mail = self.store.files[0]
        mail.write_bytes(b"From new\nSubject: Updated\n\nUpdated body\n")
        self.browser.refresh()
        self.assertEqual(self.browser.results[0].subject, "Updated")
        self.browser.query = "all:Updated"
        with patch.object(self.store, "_ensure_search_text", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.browser.refresh()
        self.assertEqual(len(self.browser.results), 1)
        self.assertEqual(self.browser.results[0].subject, "Updated")


if __name__ == "__main__":
    unittest.main()
