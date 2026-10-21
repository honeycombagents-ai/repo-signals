#!/usr/bin/env python3
"""Fail on secrets and stray identifiers in the repository.

Scans every tracked file (and, with --history, the lines each commit added).
Prints file, line and the KIND of each hit. Never prints the matched text.

Generic checks always run. Extra patterns come from the DENYLIST_PATTERNS
environment variable: one case-insensitive regex per line.

Exit status: 0 clean, 1 on any hit or on an invalid deny-list pattern.
"""
import argparse
import os
import re
import subprocess
import sys

ALLOWED_EMAIL = "noreply@honeycombagents.ai"

GENERIC_CHECKS = [
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")),
    ("GitHub token (ghp_)", re.compile(r"\bghp_[A-Za-z0-9]{30,}")),
    ("GitHub token (github_pat_)", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("GitHub token (gho_)", re.compile(r"\bgho_[A-Za-z0-9]{30,}")),
    ("Slack token (xox*)", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("sk- style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AWS ARN", re.compile(r"\barn:aws(?:-[a-z]+)*:[a-z0-9-]+:")),
    ("AWS account id", re.compile(
        r"(?i)(?:account|arn)[^\n]{0,40}?(?<![0-9])[0-9]{12}(?![0-9])"
        r"|(?<![0-9])[0-9]{12}(?![0-9])[^\n]{0,40}?(?:account|arn)")),
    ("Salesforce id", re.compile(
        r"\b(?=[0-9A-Za-z]*[A-Z])(?=[0-9A-Za-z]{3}[0-9A-Za-z]*[0-9])"
        r"(?:001|003|005|006|00D|00G|00Q|00T|00U|500|701)"
        r"(?:[0-9A-Za-z]{12}|[0-9A-Za-z]{15})\b")),
]
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def load_denylist(out):
    raw = os.environ.get("DENYLIST_PATTERNS", "")
    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    if not lines:
        print("DENYLIST_PATTERNS is not set: running the generic checks only.", file=out)
        return [], 0
    patterns, bad = [], 0
    for n, src in enumerate(lines, 1):
        try:
            patterns.append((f"deny-list pattern #{n}", re.compile(src, re.IGNORECASE)))
        except re.error:
            print(f"deny-list pattern #{n} is not a valid regex", file=out)
            bad += 1
    return patterns, bad


def line_kinds(line, deny):
    kinds = [kind for kind, rx in GENERIC_CHECKS if rx.search(line)]
    if any(m.group(0).lower() != ALLOWED_EMAIL for m in EMAIL.finditer(line)):
        kinds.append("email address")
    kinds += [kind for kind, rx in deny if rx.search(line)]
    return kinds


def git(*args):
    return subprocess.run(["git", "-c", "core.quotepath=off", *args],
                          capture_output=True, check=True).stdout


def scan_tree(deny, report):
    for path in git("ls-files", "-z").decode("utf-8", "replace").split("\0"):
        if not path or not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            blob = fh.read()
        if b"\0" in blob[:8192]:
            continue
        for n, line in enumerate(blob.decode("utf-8", "replace").splitlines(), 1):
            for kind in line_kinds(line, deny):
                report(f"{path}:{n}: {kind}")


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def scan_history(deny, report):
    try:
        log = git("log", "-p", "--no-color", "--no-ext-diff", "--unified=0",
                  "--format=commit %H", "HEAD")
    except subprocess.CalledProcessError:
        return  # no commits yet
    commit, path, n = "", "", 0
    for line in log.decode("utf-8", "replace").splitlines():
        if line.startswith("commit ") and len(line) == 47:
            commit, path = line[7:19], ""
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            m = HUNK.match(line)
            n = int(m.group(1)) if m else 0
        elif line.startswith("+") and path:
            for kind in line_kinds(line[1:], deny):
                report(f"commit {commit} {path}:{n}: {kind}")
            n += 1


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", action="store_true",
                    help="also scan the lines added by every commit reachable from HEAD")
    args = ap.parse_args(argv)
    out = sys.stdout
    deny, bad = load_denylist(out)
    hits = []

    def report(msg):
        hits.append(msg)
        print(msg, file=out)

    scan_tree(deny, report)
    if args.history:
        scan_history(deny, report)
    print(f"scan: {len(hits)} hit(s)" + (" (history included)" if args.history else ""), file=out)
    return 1 if hits or bad else 0


if __name__ == "__main__":
    sys.exit(main())
