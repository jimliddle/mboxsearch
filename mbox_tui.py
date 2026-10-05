"""A dependency-free curses mailbox browser, imported only when requested."""

import curses
from pathlib import Path
import shlex
import sqlite3
import textwrap

from mbox_index import parse_terms
from mbox_attachments import list_attachments, save_attachment
from mbox_render import message_headers, message_text, safe_text


class Browser:
    def __init__(self, store, terms=(), exact=False, field="all", log_file=None):
        self.store = store
        self.terms = terms
        self.exact = exact
        self.field = field
        self.log_file = log_file
        self.query = " ".join(shlex.quote(field + ":" + term) for term, field in terms)
        self.results = store.search(terms, exact)
        self.selected = 0
        self.clear_message()
        self.focus_body = False
        self.last_save_directory = ""
        self.status = "Enter opens a message. / searches. ? shows help."
        self.help = False

    def clear_message(self):
        self.message = None
        self.body = None
        self.body_scroll = 0
        self.attachments = []
        self.attachment_selected = 0
        self.show_attachments = False

    def move(self, delta):
        selected = max(0, min(len(self.results) - 1, self.selected + delta))
        if selected != self.selected:
            self.selected = selected
            self.clear_message()

    def apply_search(self, query, exact=None):
        tokens = shlex.split(query)
        # Explicit UI prefixes take precedence over the command-line default field.
        default = self.field if len(tokens) == 1 and ":" not in tokens[0] else "all"
        terms = parse_terms(tokens, default)
        new_exact = self.exact if exact is None else exact
        results = self.store.search(terms, new_exact)
        self.results.close()
        self.results = results
        self.query, self.terms, self.exact = query, terms, new_exact
        self.selected = self.body_scroll = 0
        self.clear_message()
        self.focus_body = False
        self.status = "{} matching messages".format(len(results))
        if self.log_file:
            self.log_results()

    def log_results(self):
        # Stream headers; logging never forces body loading.
        with open(self.log_file, "a", encoding="utf-8") as stream:
            for info in self.results:
                stream.write("Match found in message {} from {}: {}\n".format(
                    info.index, info.file, safe_text(info.subject)))

    def open_selected(self):
        if not len(self.results):
            return
        info = self.results[self.selected]
        if self.message is None:
            self.message = self.store.get_mime_message(info)
            self.attachments = list_attachments(self.message)
        label = "Attachments: {} (a lists/saves files)".format(len(self.attachments))
        self.body = message_headers(self.message) + [label, ""] + message_text(self.message, limit=200000).splitlines()
        self.body_scroll = 0
        self.focus_body = True
        self.status = "{} | message index {} | Tab returns to the list".format(
            Path(info.file).name, info.index)

    def open_attachments(self):
        if not len(self.results):
            return
        self.open_selected()
        self.show_attachments = True
        self.status = "Choose a file and press s to save, or A to save all."
        if not self.attachments:
            self.status = "No attachments in this message. Esc returns to the message."

    def save_files(self, directory, all_files=False):
        if not self.attachments:
            self.status = "No attachments in this message."
            return
        selected = self.attachments if all_files else [self.attachments[self.attachment_selected]]
        saved = []
        for attachment in selected:
            destination = save_attachment(attachment, directory)
            saved.append(destination)
            self.status = "Saved: " + str(destination)
        self.last_save_directory = str(directory)
        if all_files:
            self.status = "Saved {} files to {}".format(len(saved), Path(directory).expanduser().resolve())

    def attachment_key(self, key):
        if key in ("\x1b", "a", "\t"):
            self.show_attachments = False
        elif key in ("?", "h"):
            self.help = True
        elif key in ("s", "A", "\n", "\r", curses.KEY_ENTER) and self.attachments:
            directory = self.prompt("Save to directory: ", self.last_save_directory)
            if directory is not None and directory.strip():
                self._busy("Saving attachments...")
                self.save_files(directory, all_files=(key == "A"))
        else:
            step = max(1, self.screen.getmaxyx()[0] - 7)
            delta = {"j": 1, curses.KEY_DOWN: 1, "k": -1, curses.KEY_UP: -1,
                     " ": step, curses.KEY_NPAGE: step, curses.KEY_PPAGE: -step,
                     "g": -len(self.attachments), curses.KEY_HOME: -len(self.attachments),
                     "G": len(self.attachments), curses.KEY_END: len(self.attachments)}.get(key, 0)
            self.attachment_selected = max(0, min(len(self.attachments) - 1, self.attachment_selected + delta))

    def refresh(self):
        self.store.refresh()
        # Refresh invalidates old IDs. Keep a usable header-only list if rebuilding
        # content search text fails (for example, because the cache disk is full).
        old = self.results
        self.results = self.store.search()
        old.close()
        self.selected = self.body_scroll = 0
        self.clear_message()
        self.focus_body = False
        self.apply_search(self.query)

    def run(self, screen):
        self.screen = screen
        screen.keypad(True)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        original_progress = self.store.progress
        self.store.progress = self._progress
        try:
            while True:
                self.draw()
                key = screen.get_wch()
                if key in ("q", "Q"):
                    return
                try:
                    if self.help:
                        self.help = False
                    elif self.show_attachments:
                        self.attachment_key(key)
                    elif key in ("?", "h"):
                        self.help = True
                    elif key == "a":
                        self._busy("Loading attachments...")
                        self.open_attachments()
                    elif key == "/":
                        query = self.prompt("Search: ", self.query)
                        if query is not None:
                            self._busy("Searching...")
                            self.apply_search(query)
                    elif key == "c":
                        self.apply_search("")
                    elif key == "x":
                        self.apply_search(self.query, not self.exact)
                    elif key == "r":
                        self._busy("Refreshing mailbox index...")
                        self.refresh()
                    elif key in ("\n", "\r", curses.KEY_ENTER):
                        self._busy("Loading message...")
                        self.open_selected()
                    elif key == "\t":
                        self.focus_body = not self.focus_body
                    elif key == "\x1b":
                        self.focus_body = False
                    elif key in ("j", curses.KEY_DOWN):
                        self.scroll(1)
                    elif key in ("k", curses.KEY_UP):
                        self.scroll(-1)
                    elif key in (" ", curses.KEY_NPAGE):
                        self.scroll(self.page_size())
                    elif key == curses.KEY_PPAGE:
                        self.scroll(-self.page_size())
                    elif key in ("g", curses.KEY_HOME):
                        if self.focus_body:
                            self.body_scroll = 0
                        else:
                            self.move(-self.selected)
                    elif key in ("G", curses.KEY_END):
                        if self.focus_body:
                            self.body_scroll = max(0, len(self.wrapped_body()) - self.page_size())
                        else:
                            self.move(len(self.results))
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                    self.status = str(error)
        finally:
            self.store.progress = original_progress

    def page_size(self):
        height, _ = self.screen.getmaxyx()
        return max(1, (height - 6) // 2)

    def scroll(self, delta):
        if self.focus_body:
            self.body_scroll = max(0, min(max(0, len(self.wrapped_body()) - self.page_size()),
                                         self.body_scroll + delta))
        else:
            self.move(delta)

    def put(self, row, text, attr=0):
        height, width = self.screen.getmaxyx()
        if not 0 <= row < height or width < 2:
            return
        try:
            self.screen.addnstr(row, 0, safe_text(text), width - 1, attr)
        except curses.error:
            # Wide glyphs and the bottom-right cell can exceed curses' display bounds.
            pass

    def wrapped_body(self):
        _, width = self.screen.getmaxyx()
        lines = []
        for line in self.body or []:
            lines.extend(textwrap.wrap(line.expandtabs(4), max(1, width - 2),
                                       replace_whitespace=False) or [""])
        return lines

    def draw(self):
        screen = self.screen
        screen.erase()
        height, width = screen.getmaxyx()
        if height < 12 or width < 50:
            self.put(0, "Resize terminal to at least 50 columns x 12 rows; q quits.")
            screen.refresh()
            return
        self.put(0, "mboxsearch | {} messages | {} | {}".format(
            len(self.results), "whole word" if self.exact else "substring",
            "message" if self.focus_body else "list"), curses.A_REVERSE)
        self.put(1, "Search: " + (self.query or "(all messages)"))
        if self.help:
            lines = ["Up/Down or j/k: move in focused pane; PgUp/PgDn or Space: page",
                     "Enter: load selected message; Tab: switch list/message; Esc: list",
                     "a: list attachments; in that list, s saves one, A saves all",
                     "/: edit search (quotes supported); c: clear; x: toggle whole word",
                     "r: refresh changed/new mailboxes; g/G or Home/End: first/last",
                     "Fields: subject:, from:, to:, content:, all:; terms use AND",
                     "content/all keep legacy raw MIME matching, including headers",
                     "q: quit; any other key: close help"]
            for row, text in enumerate(lines, 3):
                self.put(row, text)
        elif self.show_attachments:
            self.put(2, "Attachments: {} | s save selected | A save all | Esc back".format(len(self.attachments)), curses.A_BOLD)
            self.put(3, " #    Type                     Filename", curses.A_DIM)
            size = max(1, height - 7)
            top = (self.attachment_selected // size) * size
            for offset, attachment in enumerate(self.attachments[top:top + size]):
                self.put(4 + offset, "{:>5} {:24} {}".format(
                    attachment.number, attachment.content_type[:24], safe_text(attachment.filename)),
                    curses.A_REVERSE if top + offset == self.attachment_selected else 0)
        else:
            size = self.page_size()
            top = (self.selected // size) * size
            self.put(2, " #    Sender                 Subject", curses.A_BOLD)
            for row, info in enumerate(self.results.page(top, size), 3):
                position = top + row - 3
                sender = safe_text(info.sender)[:22]
                text = "{:>5} {:22} {} [{}]".format(position + 1, sender,
                          safe_text(info.subject) or "(no subject)", Path(info.file).name)
                self.put(row, text, curses.A_REVERSE if position == self.selected else 0)
            divider = size + 3
            self.put(divider, "─" * (width - 1), curses.A_DIM)
            if self.body is None:
                lines = ["No matching messages. / changes the search." ] if not len(self.results) else [
                    "Enter loads the selected message. Tab switches panes."]
                if len(self.results):
                    info = self.results[self.selected]
                    lines = ["From: " + info.sender, "Subject: " + info.subject,
                             "Date: " + info.date, "Enter loads this message."]
            else:
                lines = self.wrapped_body()
                self.body_scroll = min(self.body_scroll, max(0, len(lines) - size))
                lines = lines[self.body_scroll:]
            for row, line in enumerate(lines[:height - divider - 3], divider + 1):
                self.put(row, line)
        self.put(height - 2, self.status, curses.A_DIM)
        footer = ("↑↓ choose  s save  A save all  Esc back  ? help  q quit" if self.show_attachments
                  else "↑↓ move  Enter read  Tab pane  a attachments  / search  ? help  q quit")
        self.put(height - 1, footer,
                 curses.A_REVERSE)
        screen.refresh()

    def _busy(self, text):
        height, _ = self.screen.getmaxyx()
        self.put(height - 2, text.ljust(self.screen.getmaxyx()[1] - 1), curses.A_REVERSE)
        self.screen.refresh()

    def _progress(self, phase, path, done, total):
        self._busy("{} {}: {}/{}".format(phase, Path(path).name, done, total))

    def prompt(self, label, initial=""):
        value = initial
        try:
            curses.curs_set(1)
        except curses.error:
            pass
        try:
            while True:
                height, width = self.screen.getmaxyx()
                self.screen.move(height - 1, 0)
                self.screen.clrtoeol()
                self.put(height - 1, label + value[-max(1, width - len(label) - 2):])
                self.screen.refresh()
                key = self.screen.get_wch()
                if key in ("\n", "\r", curses.KEY_ENTER):
                    return value
                if key == "\x1b":
                    return None
                if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
                    value = value[:-1]
                elif key == "\x15":
                    value = ""
                elif isinstance(key, str) and key.isprintable():
                    value += key
        finally:
            try:
                curses.curs_set(0)
            except curses.error:
                pass


def run_tui(store, terms=(), exact=False, field="all", log_file=None):
    browser = Browser(store, terms, exact, field, log_file)
    if log_file:
        browser.log_results()
    try:
        curses.wrapper(browser.run)
    except curses.error as error:
        raise RuntimeError("Terminal browser could not start; try --plain: " + str(error)) from error
    finally:
        browser.results.close()
