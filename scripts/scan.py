#!/usr/bin/env python3
"""Fail on secrets and stray identifiers in the repository.

Scans every tracked file and, with --history, every commit reachable from any ref (branches,
tags, remotes, notes): the lines each commit added, its message, and its author and committer;
every annotated tag's message and tagger; every ref name; and any tree or blob a tag points to.
Prints file or commit, line and the KIND of each hit. Never prints the matched text: a file
name that itself matches is shown as a short digest.

History is read by structure, never by line prefix: commits come from ``git rev-list --all``,
raw objects from ``git cat-file --batch``, and changed files from NUL-delimited
``git diff-tree -r -z`` output, so no content line can be taken for a header and no file name is
ever quoted. A line counts as added when its exact text is not in the parent's version of the
file (for a merge, in no parent's version); every other line was scanned where it was added.

Files with NUL bytes are not skipped: every file is decoded as UTF-8 with replacement, and also
as UTF-16 when it starts with a byte-order mark. A UTF-16 file that cannot be decoded is
reported as a hit, never skipped.

Generic checks always run. Extra patterns come from the DENYLIST_PATTERNS environment variable:
one case-insensitive regex per line.

Exit status: 0 clean, 1 on any hit or on an invalid deny-list pattern.
"""
import argparse
import hashlib
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
UTF16_BOMS = (b"\xff\xfe", b"\xfe\xff")
GITLINK_MODE = "160000"
NO_OBJECT = "0" * 40


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


def text_views(blob):
    """``(views, problems)``: each view is ``(suffix, lines)``.

    UTF-8 with replacement always (so NUL bytes never hide a file), plus UTF-16 when the blob
    starts with a byte-order mark. ``problems`` names anything that could not be decoded.
    """
    views = [("", blob.decode("utf-8", "replace").splitlines())]
    problems = []
    if blob.startswith(UTF16_BOMS):
        try:
            views.append((" (UTF-16)", blob.decode("utf-16").splitlines()))
        except UnicodeDecodeError:
            problems.append("undecodable UTF-16 text")
    return views, problems


def path_label(path, deny, report, where):
    """The path to print, or a digest if the path itself holds a hit (reported once here)."""
    kinds = line_kinds(path, deny)
    if not kinds:
        return path
    label = "(path " + hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:12] + ")"
    for kind in kinds:
        report(f"{where}{label}: file name: {kind}")
    return label


def scan_lines(prefix, lines, numbers, deny, report, suffix=""):
    for n in numbers:
        for kind in line_kinds(lines[n], deny):
            report(f"{prefix}:{n + 1}: {kind}{suffix}")


def scan_blob(prefix, blob, deny, report):
    """Every line of ``blob``, in every text view."""
    views, problems = text_views(blob)
    for suffix, lines in views:
        scan_lines(prefix, lines, range(len(lines)), deny, report, suffix)
    for problem in problems:
        report(f"{prefix}:0: {problem}")


def scan_tree(deny, report):
    for path in git("ls-files", "-z").decode("utf-8", "replace").split("\0"):
        if not path or not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            blob = fh.read()
        scan_blob(path_label(path, deny, report, ""), blob, deny, report)


class Objects:
    """Raw git objects through one ``git cat-file --batch`` process."""

    def __init__(self):
        self._proc = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE)

    def read(self, sha):
        """``(type, bytes)``, or ``("missing", b"")``."""
        self._proc.stdin.write(sha.encode("ascii") + b"\n")
        self._proc.stdin.flush()
        header = self._proc.stdout.readline().decode("ascii", "replace").split()
        if len(header) != 3:
            return "missing", b""
        size = int(header[2])
        data = self._proc.stdout.read(size)
        self._proc.stdout.read(1)  # the newline after every object
        return header[1], data

    def close(self):
        self._proc.stdin.close()
        self._proc.wait()


def parse_object(data):
    """``(headers, message)`` of a raw commit or tag: headers as (key, value) in order."""
    head, _, message = data.partition(b"\n\n")
    headers = []
    for line in head.split(b"\n"):
        if line.startswith(b" ") and headers:  # a continuation line (a signature, say)
            continue
        key, _, value = line.partition(b" ")
        headers.append((key.decode("ascii", "replace"), value.decode("utf-8", "replace")))
    return headers, message.decode("utf-8", "replace")


def identity(value):
    """``Name <email>`` from an author, committer or tagger header (time and zone dropped)."""
    parts = value.rsplit(" ", 2)
    return parts[0] if len(parts) == 3 else value


def scan_object_text(where, headers, message, deny, report):
    for key, value in headers:
        if key in ("author", "committer", "tagger"):
            for kind in line_kinds(identity(value), deny):
                report(f"{where} {key}: {kind}")
    for n, line in enumerate(message.splitlines(), 1):
        for kind in line_kinds(line, deny):
            report(f"{where} message:{n}: {kind}")


def changed_files(parent, commit):
    """``{path: (old_sha, new_sha)}`` for files added or changed in ``commit`` against ``parent``
    (None for a root commit), from NUL-delimited raw output: names are never quoted."""
    args = ["diff-tree", "-r", "-z", "-M", "--no-commit-id"]
    args += ["--root", commit] if parent is None else [parent, commit]
    fields = git(*args).decode("utf-8", "replace").split("\0")
    files, i = {}, 0
    while i < len(fields) and fields[i]:
        meta = fields[i][1:].split()   # :srcmode dstmode srcsha dstsha status
        status = meta[4]
        if status[0] in "RC":
            path, i = fields[i + 2], i + 3
        else:
            path, i = fields[i + 1], i + 2
        if status[0] == "D" or meta[1] == GITLINK_MODE:
            continue
        files[path] = (meta[2] if meta[0] != GITLINK_MODE else NO_OBJECT, meta[3])
    return files


def scan_history(deny, report):
    shas = git("rev-list", "--all").decode("ascii").split()
    objects = Objects()
    try:
        for sha in shas:
            kind, data = objects.read(sha)
            if kind != "commit":
                continue
            headers, message = parse_object(data)
            short = sha[:12]
            scan_object_text(f"commit {short}", headers, message, deny, report)
            parents = [v for k, v in headers if k == "parent"] or [None]
            per_parent = [changed_files(p, sha) for p in parents]
            for path in sorted(set().union(*per_parent)):
                if any(path not in files for files in per_parent):
                    continue  # identical to one parent: nothing new in this file
                scan_added(objects, f"commit {short} ", path,
                           [files[path] for files in per_parent], deny, report)
        scan_refs(objects, deny, report)
    finally:
        objects.close()


def scan_added(objects, where, path, pairs, deny, report):
    """Lines of the new version absent from every parent's version, in every text view."""
    new_views, problems = text_views(objects.read(pairs[0][1])[1])
    old_views = [text_views(objects.read(old)[1])[0] if old != NO_OBJECT else []
                 for old, _ in pairs]
    prefix = path_label(path, deny, report, where)
    for v, (suffix, lines) in enumerate(new_views):
        seen = [set(views[v][1]) if v < len(views) else set() for views in old_views]
        numbers = [n for n, line in enumerate(lines) if not any(line in s for s in seen)]
        scan_lines(where + prefix, lines, numbers, deny, report, suffix)
    for problem in problems:
        report(f"{where}{prefix}:0: {problem}")


def scan_refs(objects, deny, report):
    """Ref names, annotated tags (message and tagger), and trees or blobs a ref points at."""
    refs = git("for-each-ref", "--format=%(objectname) %(refname)").decode("utf-8", "replace")
    for n, line in enumerate(refs.splitlines(), 1):
        sha, _, name = line.partition(" ")
        for kind in line_kinds(name, deny):
            report(f"ref #{n} name: {kind}")
        seen = set()
        while sha not in seen:
            seen.add(sha)
            kind, data = objects.read(sha)
            if kind == "tag":
                headers, message = parse_object(data)
                scan_object_text(f"tag {sha[:12]}", headers, message, deny, report)
                sha = next((v for k, v in headers if k == "object"), sha)
            elif kind == "blob":
                scan_blob(f"ref #{n} blob", data, deny, report)
            elif kind == "tree":
                listing = git("ls-tree", "-r", "-z", sha).decode("utf-8", "replace")
                for entry in filter(None, listing.split("\0")):
                    meta, _, path = entry.partition("\t")
                    mode, otype, blob_sha = meta.split()
                    if otype == "blob":
                        label = path_label(path, deny, report, f"ref #{n} tree ")
                        scan_blob(f"ref #{n} tree {label}", objects.read(blob_sha)[1], deny,
                                  report)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--history", action="store_true",
                    help="also scan every commit reachable from any ref: added lines, messages "
                         "and identities, plus tags and ref names")
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
