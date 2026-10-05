"""Reproducible cold/warm comparison with the original sequential scan design."""

import argparse
import email
import json
from pathlib import Path
import sys
import tempfile
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mbox_index import MailboxStore
from mbox_search import check_term


def legacy_search(path, term, field):
    matches = 0
    buffer = []
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if line.startswith("From ") and buffer:
                message = email.message_from_string("".join(buffer))
                matches += check_term(message, term, field, False)
                buffer = []
            buffer.append(line)
        if buffer:
            matches += check_term(email.message_from_string("".join(buffer)), term, field, False)
    return matches


def timed(action, repeats=1):
    start = perf_counter()
    result = None
    for _ in range(repeats):
        result = action()
    return (perf_counter() - start) / repeats, result


def run(messages, body_bytes, repeats):
    with tempfile.TemporaryDirectory(prefix="mboxsearch-benchmark-") as directory:
        root = Path(directory)
        path = root / "large.mbox"
        line = b"Ordinary archive text with no matching keyword.\n"
        body = (line * (body_bytes // len(line) + 1))[:body_bytes] + b"\n"
        with path.open("wb") as stream:
            for number in range(messages):
                subject = "needle" if number % 1000 == 0 else "ordinary"
                stream.write(("From sender@example.com Sat Jan 01 00:00:00 2022\n"
                              "From: sender@example.com\nTo: reader@example.com\n"
                              "Subject: {} {}\nDate: 1 Jan 2022\n\n".format(subject, number)).encode())
                stream.write(body)
        values = {"messages": messages, "mbox_mib": round(path.stat().st_size / 1024**2, 2)}
        values["legacy_header_seconds"], expected = timed(lambda: legacy_search(path, "needle", "subject"), repeats)
        values["legacy_content_seconds"], content_expected = timed(lambda: legacy_search(path, "needle", "all"), repeats)
        values["cold_index_seconds"], store = timed(lambda: MailboxStore(path, root / "cache"))
        with store:
            def query(field):
                result = store.search([("needle", field)])
                count = len(result)
                result.close()
                return count
            values["warm_header_seconds"], actual = timed(lambda: query("subject"), repeats)
            assert actual == expected
            values["first_content_seconds"], actual = timed(lambda: query("all"))
            assert actual == content_expected
            values["warm_content_seconds"], actual = timed(lambda: query("all"), repeats)
            assert actual == content_expected
            values["last_message_seconds"], _ = timed(lambda: store.get_message(store.at_index(messages - 1)), repeats)
            values["cache_mib"] = round(store.cache_path.stat().st_size / 1024**2, 2)
        values["warm_open_seconds"], reopened = timed(lambda: MailboxStore(path, root / "cache"))
        reopened.close()
        values["header_speedup"] = round(values["legacy_header_seconds"] / values["warm_header_seconds"], 1)
        values["content_speedup"] = round(values["legacy_content_seconds"] / values["warm_content_seconds"], 1)
        return values


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=10000)
    parser.add_argument("--body-bytes", type=int, default=4096)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if min(args.messages, args.body_bytes, args.repeats) < 1:
        parser.error("all parameters must be positive")
    print(json.dumps(run(args.messages, args.body_bytes, args.repeats), indent=2))
