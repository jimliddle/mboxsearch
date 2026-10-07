# mboxsearch

Search and browse mbox email archives, including large Google Takeout exports,
in a visual terminal email client. A persistent SQLite index stores message
offsets and headers; message bodies are loaded when you open them.

## Requirements

Python 3.9 or newer. There are **no required third-party packages**: SQLite,
email parsing, and the plain browser use the Python standard library. The
previous `tqdm` dependency is no longer needed.

The visual browser uses `curses`, normally included with Python on macOS/Linux.
On Windows, install the optional dependency with
`python -m pip install windows-curses`, or use `--plain` / `--list`. Python builds
without curses automatically fall back to the plain browser. `--tui` reports an
actionable error if curses is missing.

## Browse an archive

```sh
python mbox_search.py /path/to/Takeout/Mail
python mbox_search.py /path/to/archive.mbox --tui
```

The visual browser opens automatically on an interactive terminal with `TERM`
set. It shows a paged sender/subject list, the selected message's headers, and
a scrollable reading pane. Press Enter to load that message's body. The source
mbox files are opened read-only.

| Key | Action |
| --- | --- |
| Up/Down, `j`/`k` | Move in the focused pane |
| Enter | Open the selected message |
| `a` | List the selected message's attachments |
| `s`, Enter (attachment list) | Save the selected attachment to a directory |
| `A` (attachment list) | Save all attachments from this message |
| Escape, `a`, Tab (attachment list) | Return to the message |
| Tab | Switch between message list and reading pane |
| Escape | Return focus to the list; cancel a search edit |
| Page Up/Page Down, Space | Scroll by a page (Space goes forward) |
| `g`/`G`, Home/End | First/last message or start/end of text |
| `/` | Edit search; Enter applies it; Ctrl-U clears the input |
| `c` | Clear the search and browse all messages |
| `x` | Toggle whole-word matching |
| `r` | Refresh changed files and discover new/removed mboxes |
| `?`, `h` | Show help |
| `q` | Quit |

The reading pane decodes MIME charsets and encoded headers, prefers plain text,
and converts HTML-only mail to text without loading remote content. The reading
pane shows an attachment count; press `a` to list filenames and MIME types,
then `s` to save a file or `A` to save all. Attachment data is omitted from the
body preview. Terminal control characters are filtered.
Very long previews stop at 200,000 characters; use `--view` to print the full
readable text. Resize support includes a prompt for terminals smaller than
50 columns by 12 rows.

## Search

Existing command lines remain supported:

```sh
python mbox_search.py /path/to/Mail "anyemail@example.com" --field all
python mbox_search.py /path/to/Mail "from:someone@example.com" "subject:license" "content:important"
python mbox_search.py /path/to/Mail "subject:license" --exact --log search_results.log
python mbox_search.py /path/to/Mail "subject:license" --plain
python mbox_search.py /path/to/archive.mbox "subject:license" --list
```

On Windows, quote paths containing spaces, for example
`python mbox_search.py "C:\Users\Takeout\Mail" "subject:license" --plain`.

All terms must match (logical AND). Fields are `all`, `subject`, `from`, `to`,
and `content`. Searches are case-insensitive literal substrings, not regular
expressions. `--exact` retains the original `\bterm\b` whole-word behavior;
it does not require an entire field to equal the term. `--field` applies to a
single command-line term, as before; use explicit prefixes for multiple terms.
In the visual search input, use quotes for phrases, such as
`subject:"license renewal" from:alice`.

Combine a sender, date range, and optional keyword in the visual search box:

```text
from:"John Smith" after:2024-01-01 before:2025-01-01
from:"John Smith" after:2024-01-01 before:2025-01-01 content:invoice
```

The same filters work on the command line:

```sh
python mbox_search.py /path/to/archive.mbox "from:john@example.com" "after:2024-01-01" "before:2025-01-01" --list
```

`after:YYYY-MM-DD` includes that day; `before:YYYY-MM-DD` excludes that day.
Either bound can be used on its own, and all filters must match. Boundaries use
UTC calendar days, converting timezone-aware message dates to UTC. Dates without
a timezone are treated as UTC. Messages with missing or invalid `Date` headers
are excluded only when a date filter is present. Invalid search dates produce
an error; `--exact` affects text matching only.

Date filters read headers already stored in existing caches. **Updating the
program does not require re-indexing or rebuilding the content cache**, and the
cache schema remains version 1. Sender/date and subject/date searches do not
build or scan the content cache. Adding `content:` or an unprefixed keyword uses
the shared content cache as usual, with the date filter checked before matching
the message text. Date headers are parsed during each filtered search, so date
filtering adds some work proportional to the number of indexed messages.

For compatibility, **`all` and `content` both search the serialized raw MIME
message, including headers and encoded attachment payloads**. Field searches
also retain raw header matching. Display decoding does not change search
semantics: `Caf=E9` matches a quoted-printable body, while `Café` need not.
No FTS word tokenizer is used, so short substrings, punctuation, Unicode
case-insensitive matching, and whole-word rules retain their previous behavior.

`--plain` keeps the numbered text browser, with 50 results per page and `n`/`p`
navigation. Numbers may select a result from any page. `--list` prints matching
headers and exits. Redirected input/output defaults to this noninteractive mode,
so scripts and pipes never wait at a prompt. `--log` appends matching summaries
without loading their bodies; visual searches also append their new results.

## View by index

```sh
python mbox_search.py /path/to/Mail --view 128
python mbox_search.py /path/to/archive.mbox --view 128 > message.txt
```

`--view` remains **zero-based within each file** and opens the first file
containing that index. Files are now visited in deterministic sorted path order.
Result-list numbers are one-based across the current search and are distinct
from these per-file indexes. Opening a message seeks directly to its byte range.

## Retrieve attachments

In the visual browser, select a message and press `a`. Use Up/Down to choose an
attachment, then `s` (or Enter) to save it. `A` saves all attachments from that
message. Enter the destination directory at the prompt; Escape cancels. The
status line shows the saved path. You can then open the file with its normal
application. Files are not launched automatically.

Command-line attachment access also works without curses:

```sh
python mbox_search.py /path/to/archive.mbox --view 128 --attachments
python mbox_search.py /path/to/archive.mbox --view 128 --save-attachments ./saved-files
python mbox_search.py /path/to/archive.mbox --view 128 --save-attachments ./saved-files --attachment 2
```

`--attachments` lists files without printing the message body. `--save-attachments`
exports all files by default; `--attachment` selects a **one-based** attachment
number from that list. Both listing and export require `--view`; use a single
mbox path to identify the exact file when several mailboxes contain that index.

Base64 and quoted-printable payloads are decoded when saved. Byte-based MIME
parsing preserves binary payloads, including 8-bit attachments. Inline images
and unnamed non-text MIME parts are also listed; unnamed files get generated
names. Attached emails are saved as `.eml` MIME messages, with their nested
attachments included inside that file.

Only the selected email is loaded; browsing does not decode attachments, and
attachment export does not build the archive's full content-search cache.
Large individual messages and attachment decoding can still use substantial
memory. Search semantics remain unchanged: attachment documents are not parsed
for searchable PDF/Word text.

Saved names are made portable by stripping directory components, control
characters, and reserved filename characters. Existing files are never
overwritten: duplicate names become `report (2).pdf`, `report (3).pdf`, etc.
Exports use private permissions on POSIX systems and leave the source mbox
unchanged.

## Index design and performance

- The first open makes one sequential pass to record byte ranges and parse
  headers. It supports LF, CRLF, and CR line endings and retains the original
  rule that any line beginning `From ` starts a message. Standard mbox escaping
  (`>From ` in bodies) is therefore required, as before.
- Subsequent opens validate filesystem metadata and reuse the index. Browsing
  and `subject`/`from`/`to` searches never parse message bodies.
- The first `all`/`content` search parses messages once and stores their
  serialized search text in SQLite. Future content searches scan that text
  without reparsing the mbox. This first search can take about as long as the
  original scan, plus cache-writing time.
- Header and content substring searches are still linear in the relevant
  cached text. Content search is not constant-time, and its cache can be about
  the size of the original archive. Avoiding a token-based FTS index preserves
  existing matching behavior.
- Result IDs live in a disk-backed temporary table. Only the visible page's
  headers and one selected message are loaded. Memory does not scale with the
  number of matching emails; unusually large individual messages or headers
  can still require significant memory.
- Appends, truncations, replacements, and metadata-changing edits invalidate
  the affected file's index and content cache. Rebuilding currently scans that
  whole file; it is not an incremental append index. Transactions protect
  existing cache records if indexing or search-text generation is interrupted.
  Changes during an active session are detected before searches/reads; press
  `r` or reopen. Validation uses file identity, size, mtime, and ctime, not a full
  content hash. Edits preserving every checked metadata value require
  `--reindex` (including restored timestamps on some filesystems).

Run the reproducible benchmark described in [benchmarks/README.md](benchmarks/README.md).
It compares the previous sequential parsing approach with cold indexing, warm
opening, header searches, content searches, and reading the last message.

## Cache management

The default cache is `$XDG_CACHE_HOME/mboxsearch`, or `~/.cache/mboxsearch`.
On Windows it uses `%LOCALAPPDATA%/mboxsearch` when available. Each canonical
input path has a separate database. The cache directory and new databases are
created with private permissions on POSIX systems.

```sh
python mbox_search.py /path/to/archive.mbox --cache-dir /private/cache/mboxsearch
python mbox_search.py /path/to/archive.mbox --reindex
```

The cache contains email headers and, after content searches, copies of raw
message content. Store it somewhere private with enough free disk space.
Indexes may be deleted while the browser is closed; they will rebuild next
time. Deleting the database also reclaims disk space after shrinking an archive;
`--reindex` rebuilds records but does not compact SQLite's allocated pages.

The original `search_mbox`, `process_message`, `check_term`, `view_message`,
`log_message`, and `view_specific_message` helpers remain importable. The legacy
`search_mbox` API still returns `(file, index, Message)` tuples and consequently
loads every matching message. New code should use `MailboxStore.search()` and
`Results.page()` for bounded-memory access.

## Development

```sh
python -m unittest discover -s tests -v
python benchmarks/benchmark.py --messages 10000 --body-bytes 4096
```

Tests cover old matching semantics, lazy parsing, cache reuse/invalidation,
interrupted builds, byte offsets and newline formats, pagination, CLI modes,
MIME rendering, binary attachment decoding/export, and terminal browser behavior.
CI runs on Linux, macOS, and
Windows with Python 3.9 and 3.13; curses-specific tests skip when unavailable.
