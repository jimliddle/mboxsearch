import email
from email.message import EmailMessage
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mbox_attachments import export_attachments, list_attachments, safe_filename, save_attachment
from mbox_index import MailboxStore
from mbox_render import message_text
from mbox_search import main


def sample_message():
    message = EmailMessage()
    message["From"] = "alice@example.com"
    message["Subject"] = "Files attached"
    message.set_content("Body contents")
    message.add_attachment(b"\x00\xff\x80PDF", maintype="application", subtype="pdf", filename="report.pdf")
    message.add_attachment("Café notes", subtype="plain", charset="utf-8", cte="quoted-printable", filename="café.txt")
    message.add_attachment(b"PNG bytes", maintype="image", subtype="png", filename="picture.png", disposition="inline")
    return message


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_listing_is_lazy_and_decoding_preserves_bytes(self):
        message = email.message_from_bytes(sample_message().as_bytes())
        part = message.get_payload()[1]
        with patch.object(part, "get_payload", side_effect=AssertionError("decoded during listing")):
            files = list_attachments(message)
        self.assertEqual([item.filename for item in files], ["report.pdf", "café.txt", "picture.png"])
        self.assertEqual(files[0].data(), b"\x00\xff\x80PDF")
        self.assertEqual(files[1].data(), "Café notes\n".encode("utf-8"))
        self.assertEqual(files[2].data(), b"PNG bytes")
        self.assertNotIn("Café notes", message_text(message))

    def test_encoded_filename_unnamed_attachment_and_attached_message(self):
        message = EmailMessage()
        message.set_content("Cover note")
        child = EmailMessage()
        child["Subject"] = "Forwarded mail"
        child.set_content("Forwarded body")
        child.add_attachment(b"nested", maintype="application", subtype="octet-stream", filename="nested.bin")
        message.add_attachment(child, filename="forward.eml")
        message.add_attachment(b"unnamed", maintype="application", subtype="octet-stream")
        parsed = email.message_from_bytes(message.as_bytes())
        files = list_attachments(parsed)
        self.assertEqual(len(files), 2)
        self.assertEqual(files[0].filename, "forward.eml")
        forwarded = email.message_from_bytes(files[0].data())
        self.assertEqual(forwarded["Subject"], "Forwarded mail")
        self.assertEqual(list_attachments(forwarded)[0].data(), b"nested")
        self.assertTrue(files[1].filename.startswith("attachment-2."))
        encoded = email.message_from_bytes(
            b"Content-Type: application/pdf\nContent-Disposition: attachment; "
            b"filename=\"=?utf-8?b?Y2Fmw6kucGRm?=\"\n\nfile")
        self.assertEqual(list_attachments(encoded)[0].filename, "café.pdf")

    def test_filenames_stay_in_chosen_directory_and_collisions_are_preserved(self):
        destination = self.root / "saved"
        destination.mkdir()
        existing = destination / "report.pdf"
        existing.write_bytes(b"Existing document")
        message = sample_message()
        part = message.get_payload()[1]
        part.replace_header("Content-Disposition", 'attachment; filename="../../report.pdf"')
        file = list_attachments(message)[0]
        first = save_attachment(file, destination)
        second = save_attachment(file, destination)
        self.assertEqual(first.name, "report (2).pdf")
        self.assertEqual(second.name, "report (3).pdf")
        self.assertEqual(first.parent, destination.resolve())
        self.assertEqual(existing.read_bytes(), b"Existing document")
        self.assertEqual(first.read_bytes(), file.data())
        if os.name != "nt":
            self.assertEqual(first.stat().st_mode & 0o777, 0o600)
        for name, expected in [(r"C:\Users\mail\file.txt", "file.txt"),
                               ("../../../file.txt", "file.txt"), ("..", "attachment.bin"),
                               ("CON.txt", "_CON.txt"), ("NUL", "_NUL"),
                               ("bad\x00:name?.pdf", "bad_name_.pdf")]:
            self.assertEqual(safe_filename(name), expected)
        self.assertLessEqual(len(safe_filename("é" * 500).encode("utf-8")), 180)

    def test_existing_symlink_is_not_followed(self):
        if os.name == "nt":
            self.skipTest("Windows symlink creation may require additional privileges")
        target = self.root / "outside.pdf"
        target.write_bytes(b"Original")
        folder = self.root / "saved"
        folder.mkdir()
        (folder / "report.pdf").symlink_to(target)
        exported = save_attachment(list_attachments(sample_message())[0], folder)
        self.assertEqual(exported.name, "report (2).pdf")
        self.assertEqual(target.read_bytes(), b"Original")

    def test_selected_export_and_invalid_numbers(self):
        message = sample_message()
        files = list(export_attachments(message, self.root / "single", 2))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "café.txt")
        with self.assertRaises(ValueError):
            list(export_attachments(message, self.root / "invalid", 99))
        self.assertFalse((self.root / "invalid").exists())
        self.assertEqual(list(export_attachments(EmailMessage(), self.root / "empty")), [])

    def test_store_binary_payload_and_lazy_export_without_content_cache(self):
        raw = (b"From sender\nSubject: Binary attachment\nContent-Type: application/octet-stream\n"
               b"Content-Disposition: attachment; filename=raw.bin\n"
               b"Content-Transfer-Encoding: 8bit\n\n\x00\xff\x80bytes")
        mailbox = self.root / "raw.mbox"
        mailbox.write_bytes(raw)
        with MailboxStore(mailbox, self.root / "cache") as store:
            with patch("mbox_index.email.message_from_bytes", wraps=email.message_from_bytes) as parse:
                matches = store.search()
                parse.assert_not_called()
                message = store.get_mime_message(matches[0])
                self.assertEqual(parse.call_count, 1)
                paths = list(export_attachments(message, self.root / "saved"))
                self.assertEqual(paths[0].read_bytes(), b"\x00\xff\x80bytes")
            self.assertEqual(store.db.execute("SELECT count(*) FROM search_text").fetchone()[0], 0)
        self.assertEqual(mailbox.read_bytes(), raw)

    def test_cli_list_export_all_select_one_and_errors(self):
        mailbox = self.root / "mail.mbox"
        raw = b"From sender\n" + sample_message().as_bytes()
        mailbox.write_bytes(raw)
        common = [str(mailbox), "--cache-dir", str(self.root / "cache"), "--view", "0"]
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(main(common + ["--attachments"]), 0)
            self.assertIn("report.pdf", output.getvalue())
            self.assertIn("café.txt", output.getvalue())
            self.assertNotIn("Body contents", output.getvalue())
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(common + ["--save-attachments", str(self.root / "all")]), 0)
            self.assertEqual(main(common + ["--save-attachments", str(self.root / "one"), "--attachment", "1"]), 0)
        self.assertEqual(len(list((self.root / "all").iterdir())), 3)
        self.assertEqual((self.root / "one" / "report.pdf").read_bytes(), b"\x00\xff\x80PDF")
        with patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(common + ["--save-attachments", str(self.root / "bad"), "--attachment", "99"]), 1)
            for argv in ([str(mailbox), "--attachments"], common + ["--attachment", "1"],
                         common + ["--save-attachments", "files", "--attachment", "0"]):
                with self.assertRaises(SystemExit):
                    main(argv)
        self.assertEqual(mailbox.read_bytes(), raw)


if __name__ == "__main__":
    unittest.main()
