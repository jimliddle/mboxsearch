"""Readable MIME text and safe terminal output."""

from html.parser import HTMLParser
import unicodedata

from mbox_index import display_header


def safe_text(text, multiline=False):
    text = str(text)
    if not multiline:
        text = " ".join(text.splitlines())
    return "".join(character for character in text
                   if (multiline and character in "\n\t")
                   or not unicodedata.category(character).startswith("C"))


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("br", "p", "div", "li", "tr") and not self.hidden:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _body_parts(message):
    if message.get_content_disposition() == "attachment" or message.get_filename():
        return
    if message.is_multipart():
        for part in message.get_payload():
            yield from _body_parts(part)
    else:
        yield message


def _decode_part(part):
    payload = part.get_payload(decode=True)
    if payload is None:
        return str(part.get_payload() or "")
    try:
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def message_text(message, limit=None):
    parts = list(_body_parts(message))
    selected = [part for part in parts if part.get_content_type() == "text/plain"]
    html = not selected
    if html:
        selected = [part for part in parts if part.get_content_type() == "text/html"]
    output, remaining = [], limit
    truncated = False
    for part in selected:
        text = _decode_part(part)
        if html:
            parser = _HTMLText()
            parser.feed(text)
            text = "".join(parser.parts)
        if remaining is not None:
            truncated = truncated or len(text) > remaining
            text = text[:remaining]
            remaining -= len(text)
        output.append(text)
        if remaining == 0:
            truncated = truncated or part is not selected[-1]
            break
    text = "\n\n".join(output) or "[No readable text body]"
    if truncated:
        text += "\n\n[Preview truncated; use --view for the full text]"
    return safe_text(text, multiline=True)


def message_headers(message):
    return [name + ": " + safe_text(display_header(message.get(name)))
            for name in ("From", "To", "Subject", "Date")]
