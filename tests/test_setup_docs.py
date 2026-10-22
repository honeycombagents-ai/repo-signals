"""The setup guide and the troubleshooting table: headings, shell blocks, and codes that match src/."""
import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
GUIDE = ROOT / "docs/setup-guide.md"
TROUBLE = ROOT / "docs/troubleshooting.md"
FAMILIES = {"ProviderError", "RateLimitedError", "SignalError", "PlanError", "DraftError",
            "ConfigError"}
CODE_RE = re.compile(r"[a-z{][a-z0-9_{}]*\Z")
DROP_TABLE = "## People-stage drop counts\n"
CODE_TABLES = ("## Error codes\n", "## Drops and outcomes\n", DROP_TABLE)


def _strings(node):
    """String constants in ``node``; an f-string becomes its text with ``{}`` for each value."""
    if isinstance(node, ast.JoinedStr):
        yield "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
        return
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
        return
    for child in ast.iter_child_nodes(node):
        yield from _strings(child)


def _trees():
    return {p: ast.parse(p.read_text()) for p in sorted(SRC.rglob("*.py"))}


def src_literals():
    """Every string literal under src/, f-strings included in their ``{}`` form."""
    found = set()
    for tree in _trees().values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Constant, ast.JoinedStr)):
                found.update(_strings(node))
    return found


def raised_codes():
    """Code-only strings raised through the error families, directly or through a helper.

    Covers ``ProviderError("code")``, codes returned by (or defaulted in) a helper whose result
    is passed to a family, and codes passed into a helper that raises its own parameter.
    """
    trees = _trees()
    codes, helpers, passthrough = set(), set(), set()
    for tree in trees.values():
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in FAMILIES and node.args):
                continue
            arg = node.args[0]
            codes.update(_strings(arg))
            helpers.update(c.func.id for c in ast.walk(arg)
                           if isinstance(c, ast.Call) and isinstance(c.func, ast.Name))
            if isinstance(arg, ast.Name):
                f = parents.get(node)
                while f is not None and not isinstance(f, ast.FunctionDef):
                    f = parents.get(f)
                params = [a.arg for a in f.args.args] if f is not None else []
                if arg.id in params:
                    passthrough.add((f.name, params.index(arg.id)))
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in helpers:
                for r in ast.walk(node):
                    if isinstance(r, ast.Return) and r.value is not None:
                        codes.update(_strings(r.value))
                for d in node.args.defaults + [d for d in node.args.kw_defaults if d]:
                    codes.update(_strings(d))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                for name, index in passthrough:
                    if node.func.id == name and len(node.args) > index:
                        codes.update(_strings(node.args[index]))
    return {c for c in codes if CODE_RE.fullmatch(c)}


def drop_codes():
    """The codes the providers count in ``last_dropped``: every ``self._drop(...)`` argument."""
    codes = set()
    for path in sorted((SRC / "repo_signals/providers").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_drop" and node.args):
                codes.update(_strings(node.args[0]))
    return codes


def table_codes(headings=CODE_TABLES):
    """The backticked first-column codes of the troubleshooting code tables."""
    text = TROUBLE.read_text()
    codes = []
    for heading in headings:
        section = text.split(heading, 1)[1].split("\n## ", 1)[0]
        for line in section.splitlines():
            m = re.match(r"\| `([^`]+)` \|", line)
            if m:
                codes.append(m.group(1))
    return codes


def normalise(code):
    """``signal_target_N_bad_domain`` and ``missing_FIELD`` become the f-string ``{}`` form."""
    return re.sub(r"[A-Z]+", "{}", code)


def shell_blocks(text):
    return re.findall(r"```(?:sh|zsh|bash)\n(.*?)```", text, re.S)


class SetupDocs(unittest.TestCase):
    def test_docs_exist_with_headings(self):
        guide = GUIDE.read_text()
        self.assertTrue(guide.startswith("# Setup guide"))
        for n, heading in enumerate(("What you need", "Install", "Create the config folder",
                                     "A throwaway Google Cloud project", "An empty sheet",
                                     "Set the variables", "A signal file", "Sender details",
                                     "doctor", "The dry run", "The first live run",
                                     "Check the sheet", "Run it again", "Clean up"), 1):
            self.assertIn(f"\n## {n}. {heading}", guide)
        trouble = TROUBLE.read_text()
        self.assertTrue(trouble.startswith("# Troubleshooting\n"))
        for heading in (*CODE_TABLES, "## Exit codes\n", "## Common mistakes\n"):
            self.assertIn("\n" + heading, trouble)

    def test_table_codes_exist_in_src(self):
        literals = src_literals()
        codes = table_codes()
        self.assertEqual(len(codes), len(set(codes)), "a code is listed twice")
        missing = sorted(c for c in codes if normalise(c) not in literals)
        self.assertEqual(missing, [], "codes in the table but not in src/")

    def test_every_raised_code_is_in_the_table(self):
        documented = {normalise(c) for c in table_codes()}
        missing = sorted(raised_codes() - documented)
        self.assertEqual(missing, [], "codes raised in src/ but missing from the table")

    def test_every_drop_count_is_in_its_table(self):
        found = drop_codes()
        self.assertEqual(found, {"missing_name", "missing_title", "filtered_by_title",
                                 "missing_email"})
        missing = sorted(found - set(table_codes((DROP_TABLE,))))
        self.assertEqual(missing, [], "drop counts missing from the drop-count table")

    def test_named_codes_are_documented(self):
        codes = set(table_codes())
        for code in ("header_missing", "header_mismatch", "not_found", "token_rejected",
                     "signing_failed", "openssl_not_found", "org_not_visible", "bad_query",
                     "rate_limited", "plan_exhausted", "budget_exhausted", "pushed_in_future"):
            self.assertIn(code, codes)

    def test_clean_text_and_pasteable_shell_blocks(self):
        for path in (GUIDE, TROUBLE):
            text = path.read_text()
            with self.subTest(file=path.name):
                self.assertIsNone(re.search("lam" + "bda", text, re.IGNORECASE))
                self.assertNotIn("TBD", text)
                self.assertNotIn("\u2014", text)
                blocks = shell_blocks(text)
                self.assertTrue(blocks)
                for block in blocks:
                    self.assertIsNone(re.search(r"<[A-Za-z][^<>\n]*>", block), block)
                    self.assertIsNone(re.search(r"(^|\s)#", block), block)
        self.assertIsNone(re.search("lam" + "bda", Path(__file__).read_text(), re.IGNORECASE))

    def test_guide_safety_notes(self):
        guide = " ".join(GUIDE.read_text().split())
        self.assertIn("Sheet1", guide)
        self.assertIn("no project roles", guide)
        self.assertIn("link sharing off", guide)
        self.assertIn("first names", guide)
        self.assertIn("approximate", guide)
        self.assertIn("refused, by design", guide)
        self.assertIn("It never sends email.", guide)
        self.assertIn("It never deletes sheet data.", guide)
