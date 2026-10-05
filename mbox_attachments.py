"""List MIME attachments and export them without overwriting existing files."""

from dataclasses import dataclass, field
from email import policy
import mimetypes
import os
from pathlib import Path
import re

from mbox_index import display_header
from mbox_render import safe_text


def safe_filename(name, fallback="attachment.bin"):
    # Mail filenames can contain paths from either platform; export only a basename.
    name = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*]', "_", safe_text(name)).strip(" .")
    name = name.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    if not name:
        name = fallback
    reserved = {"CON", "PRN", "AUX", "NUL"}
    reserved.update("COM" + str(number) for number in range(1, 10))
    reserved.update("LPT" + str(number) for number in range(1, 10))
    if name.split(".", 1)[0].upper() in reserved:
        name = "_" + name
    return name


@dataclass(frozen=True)
class Attachment:
    number: int
    filename: str
    content_type: str
    part: object = field(repr=False)

    def data(self):
        if self.content_type == "message/rfc822":
            messages = self.part.get_payload()
            if isinstance(messages, list) and len(messages) == 1:
                return messages[0].as_bytes(policy=policy.default)
        payload = self.part.get_payload(decode=True)
        if payload is not None:
            return payload
        # Attached multipart MIME containers are exported with their MIME headers.
        return self.part.as_bytes(policy=policy.default)


def list_attachments(message):
    attachments = []

    def visit(part):
        filename = part.get_filename()
        content_type = part.get_content_type()
        attached = (part.get_content_disposition() == "attachment" or filename
                    or content_type == "message/rfc822"
                    or (not part.is_multipart() and part.get_content_maintype() != "text"))
        if attached:
            number = len(attachments) + 1
            extension = ".eml" if content_type == "message/rfc822" else (
                mimetypes.guess_extension(content_type) or ".bin")
            if part.is_multipart() and content_type != "message/rfc822":
                extension = ".mime"
            filename = display_header(filename) or "attachment-{}{}".format(number, extension)
            attachments.append(Attachment(number, filename, content_type, part))
        elif part.is_multipart():
            for child in part.get_payload():
                visit(child)

    visit(message)
    return attachments


def save_attachment(attachment, directory):
    """Decode one attachment, create a private file, and return its actual path."""
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    filename = safe_filename(attachment.filename)
    stem, suffix = os.path.splitext(filename)
    payload = attachment.data()
    number = 1
    while True:
        name = filename if number == 1 else "{} ({}){}".format(stem, number, suffix)
        destination = directory / name
        try:
            descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            number += 1
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
    except BaseException:
        destination.unlink()
        raise
    return destination


def export_attachments(message, directory, number=None):
    attachments = list_attachments(message)
    if number is not None:
        if not 1 <= number <= len(attachments):
            raise ValueError("Attachment number {} not found ({} attachments)".format(number, len(attachments)))
        attachments = [attachments[number - 1]]
    # Decode/write one at a time rather than keeping all decoded files in memory.
    for attachment in attachments:
        yield save_attachment(attachment, directory)
