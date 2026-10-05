"""Search and browse mbox archives without repeatedly parsing every message."""

import argparse
import email
import logging
import os
from pathlib import Path
import sqlite3
import sys

from mbox_index import MailboxStore, compile_term, parse_terms
from mbox_attachments import export_attachments, list_attachments
from mbox_render import message_headers, message_text, safe_text


def check_term(message, term, field, exact):
    """Compatibility helper: content/all search serialized MIME, including headers."""
    pattern = compile_term(term, exact)
    if field in ("all", "content"):
        return bool(pattern.search(message.as_string()))
    if field in ("subject", "from", "to"):
        value = message.get(field)
        return value is not None and bool(pattern.search(value))
    return False


def process_message(raw_message, search_terms, results, mbox_file, index, exact, log_file=None):
    message = email.message_from_string(raw_message)
    if all(check_term(message, term, field, exact) for term, field in search_terms):
        results.append((mbox_file, index, message))
        log_message("Match found in message {} from {}: {}\n".format(
            index, mbox_file, safe_text(message.get("subject", ""))), log_file)


def search_mbox(mbox_file, search_terms, exact, log_file=None):
    """Keep the original list-of-(file, index, Message) API for existing callers.

    The CLI/TUI uses disk-backed Results instead; this legacy API necessarily
    loads all matches to preserve its return type.
    """
    results = []
    try:
        with MailboxStore(mbox_file) as store:
            matches = store.search(search_terms, exact)
            for info in matches:
                message = store.get_message(info)
                results.append((mbox_file, info.index, message))
                log_message("Match found in message {} from {}: {}\n".format(
                    info.index, mbox_file, safe_text(info.subject)), log_file)
    except (OSError, RuntimeError, sqlite3.Error) as error:
        logging.error("Error opening or reading %s: %s", mbox_file, error)
    return results


def view_message(message):
    print("\n".join(message_headers(message)))
    print("\nContent:\n" + message_text(message))
    _show_attachments(message)
    if sys.stdin.isatty() and sys.stdout.isatty():
        input("\nPress Enter to continue...")


def _show_attachments(message):
    attachments = list_attachments(message)
    if attachments:
        print("\nAttachments:")
        for attachment in attachments:
            print("{}. {} ({})".format(attachment.number, safe_text(attachment.filename), attachment.content_type))
        print("Use --view INDEX --save-attachments DIRECTORY to save these files.")
    else:
        print("\nNo attachments.")


def log_message(message, log_file=None):
    if log_file:
        with open(log_file, "a", encoding="utf-8") as stream:
            stream.write(message)
    logging.info(message.rstrip())


def view_specific_message(mbox_dir, message_index):
    with MailboxStore(mbox_dir) as store:
        info = store.at_index(message_index)
        if info is None:
            print("Message with index {} not found in any mbox file in {}".format(message_index, mbox_dir))
        else:
            view_message(store.get_mime_message(info))


def _show_results(results, start=0, limit=50):
    for number, info in enumerate(results.page(start, limit), start + 1):
        print("{}. [{}] {} (index {})".format(number, safe_text(Path(info.file).name),
              safe_text(info.subject) or "(no subject)", info.index))


def _plain_browser(store, results):
    start = 0
    while True:
        _show_results(results, start)
        choice = input("\nEmail number (1-{}), n/p page, or q: ".format(len(results))).strip().lower()
        if choice == "q":
            return
        if choice == "n":
            start = min(start + 50, ((len(results) - 1) // 50) * 50)
        elif choice == "p":
            start = max(0, start - 50)
        else:
            try:
                index = int(choice) - 1
                if index < 0:
                    raise IndexError(index)
                info = results[index]
                print("\nViewing email from {}, message index {}".format(Path(info.file).name, info.index))
                view_message(store.get_mime_message(info))
            except (ValueError, IndexError):
                print("Invalid number. Please try again.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search and browse indexed mbox email files.")
    parser.add_argument("mbox_dir", help="Directory containing .mbox files, or a single mbox file")
    parser.add_argument("search_terms", nargs="*", help="AND terms, optionally prefixed with subject:, from:, to:, content:, all:")
    parser.add_argument("--field", choices=["all", "subject", "from", "to", "content"], default="all")
    parser.add_argument("--exact", action="store_true", help="Case-insensitive whole-word matching")
    parser.add_argument("--log", help="Append matching message headers to a log file")
    parser.add_argument("--view", type=int, help="View a zero-based per-file message index")
    parser.add_argument("--attachments", action="store_true", help="List attachments for --view without printing the body")
    parser.add_argument("--save-attachments", metavar="DIRECTORY", help="Export attachments from --view into this directory")
    parser.add_argument("--attachment", type=int, metavar="NUMBER", help="Export only this one-based attachment number")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--tui", action="store_true", help="Use the visual terminal browser (default on a supported terminal)")
    modes.add_argument("--plain", action="store_true", help="Use the original numbered text browser")
    modes.add_argument("--list", action="store_true", help="Print matches and exit; default when input/output is redirected")
    parser.add_argument("--cache-dir", help="Store SQLite indexes here instead of the user cache directory")
    parser.add_argument("--reindex", action="store_true", help="Rebuild cached offsets and discard cached search text")
    args = parser.parse_args(argv)
    if args.view is not None and args.view < 0:
        parser.error("--view must be nonnegative")
    if (args.attachments or args.save_attachments is not None) and args.view is None:
        parser.error("attachment listing/export requires --view")
    if args.attachment is not None:
        if args.save_attachments is None:
            parser.error("--attachment requires --save-attachments")
        if args.attachment < 1:
            parser.error("--attachment must be at least 1")
    try:
        terms = parse_terms(args.search_terms, args.field)
    except ValueError as error:
        parser.error(str(error))

    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    terminal = os.environ.get("TERM", "") not in ("", "dumb")
    tui = args.tui or (interactive and terminal and not args.plain and not args.list)
    run_tui = None
    if tui and args.view is None:
        if not interactive or not terminal:
            parser.error("--tui needs an interactive terminal with TERM set; use --plain or --list")
        try:
            from mbox_tui import run_tui
        except ImportError:
            if args.tui:
                parser.error("curses is unavailable; on Windows install windows-curses, or use --plain")
            tui = False

    def progress(phase, path, done, total):
        if sys.stderr.isatty():
            print("\r{} {}: {}/{}".format(phase, safe_text(Path(path).name), done, total),
                  end="\n" if done == total else "", file=sys.stderr, flush=True)

    try:
        with MailboxStore(args.mbox_dir, args.cache_dir, args.reindex, progress) as store:
            if args.view is not None:
                info = store.at_index(args.view)
                if info is None:
                    print("Message with index {} not found in {}".format(args.view, args.mbox_dir))
                    return 1
                message = store.get_mime_message(info)
                if args.save_attachments is not None:
                    count = 0
                    for saved in export_attachments(message, args.save_attachments, args.attachment):
                        print("Saved: " + safe_text(saved))
                        count += 1
                    if not count:
                        print("No attachments in this message.")
                elif args.attachments:
                    print("\n".join(message_headers(message)))
                    _show_attachments(message)
                else:
                    view_message(message)
            elif tui:
                run_tui(store, terms, args.exact, args.field, args.log)
            else:
                results = store.search(terms, args.exact)
                print("Found {} matching emails.".format(len(results)))
                if args.log:
                    with open(args.log, "a", encoding="utf-8") as stream:
                        for info in results:
                            stream.write("Match found in message {} from {}: {}\n".format(
                                info.index, info.file, safe_text(info.subject)))
                if not interactive or args.list or not len(results):
                    for start in range(0, len(results), 100):
                        _show_results(results, start, 100)
                else:
                    _plain_browser(store, results)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
