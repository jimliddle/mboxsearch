# Performance benchmark

Run from the repository root:

```sh
python benchmarks/benchmark.py --messages 50000 --body-bytes 8192 --repeats 3
```

The script generates a temporary synthetic archive and cache, verifies that old
and new search paths return the same counts, and removes them when finished.
It reproduces the original delimiter scan, full message parsing, and matching
for each legacy search. Progress bars and per-match logging are excluded.
Warm search timings average three runs; cold operations are timed once.

Example measured locally on macOS with Python 3.9.6 and SQLite 3.51.0:
50,000 messages, 397.29 MiB of mbox data, one matching subject per 1,000
messages, and 8 KiB of ordinary plain text per message.

| Operation | Time |
| --- | ---: |
| Original header search (parse archive each time) | 3.130 s |
| Original content search (parse/serialize archive each time) | 8.586 s |
| First offset/header index build | 3.379 s |
| Reopen unchanged index | 0.000989 s |
| Indexed header search | 0.0124 s |
| First content search, including cache construction | 8.604 s |
| Subsequent content search | 1.507 s |
| Seek, parse, and read the final message | 0.00132 s |

Here warm header searches were **252.5× faster** and warm content searches
**5.7× faster**. The database after content caching was 422.59 MiB. Results
depend on storage speed, filesystem cache, headers, MIME complexity, attachments,
message sizes, and query terms. These are measurements of this fixture, not
guarantees for every archive or a cold operating-system disk cache.

The main gains come from avoiding repeated email parsing/serialization, reading
only metadata for header searches, and seeking to selected messages. Initial
indexing still reads the whole source; initial content caching still parses
every message. Arbitrary substring content matching still scans the cached
text and consumes disk space comparable to the archive.

The automated tests additionally assert that warm header searches parse zero
message bodies, cached content searches parse zero source messages, and opening
one selected message parses exactly one. Results are paged from a temporary
SQLite table rather than holding all matching Message objects in memory.
