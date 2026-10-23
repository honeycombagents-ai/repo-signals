"""``repo-signals run``: sheet preflight -> collect -> people -> draft -> sheet.

Dry run (the default) makes no network call and writes nothing. It runs the whole pipeline on
ONE synthetic Evidence item on example.invalid with the fake provider, and prints the stage
counts, the column layout and one sample row and draft with the email masked.

``run --live`` refuses (exit 2, no request) unless every gate in ``run_gates`` passes and stdin
and stdout are a terminal. It prints the plan and requires a typed "yes"; there is no flag to
skip that. Real clients are built only after "yes". Each service has its own CallBudget. Right
after "yes", before any GitHub or provider call, one sheet preflight reads the header row
through the writer's own client; a refused token, an unreadable sheet or another header ends the
run there (exit 4). A later stage that fails or hits its cap stops; later stages work on what
exists, and the run is partial.
Rows are only ever appended, in one append per 60 KB of new rows: a person already in the sheet
is skipped and never updated.
Nothing is ever sent by email.
"""
import os
import re
import secrets
import sys
from collections import Counter
from types import SimpleNamespace

from . import config
from .clock import utc_today
from .budget import BudgetExceeded, CallBudget
from .collector.live import CONFIRM_WORD, _is_terminal
from .collector.github_api import GitHubClient
from .collector.orchestrate import PARTIAL, REFUSED, collect, summarize
from .collector.plan import SEARCH_RATE_CEILING_PER_MINUTE
from .config import Settings
from .drafting.base import DraftError, Sender, sender_from_env
from .drafting.guard import GuardedDrafter
from .drafting.sheet import draft_for
from .drafting.suppression import suppression_from_env
from .drafting.template import TemplateDrafter
from .models import Evidence, Person
from .providers.apollo import ApolloProvider
from .providers.base import ProviderError, find_people_within
from .providers.fake import FakeProvider
from .providers.hunter import TITLES_WARNING as HUNTER_TITLES_WARNING, HunterProvider
from .providers.registry import default_registry
from .run_gates import check_gates
from .sheets.cells import mask_email
from .sheets.layout import COLUMNS
from .sheets.writer import AppendStopped, SheetWriter

PEOPLE_LIMIT = 3
# Token and header row (the preflight before collect), then the header row again, the existing
# rows, the header write and the first append. Each further append (one per 60 KB of new rows)
# adds one call; the sheets budget stays the hard limit. run_gates.MIN_SHEETS_CALLS matches it.
SHEETS_CALLS_PER_WRITE = 6
EXIT_COMPLETE, EXIT_DECLINED, EXIT_REFUSED_BEFORE_START = 0, 1, 2
EXIT_PARTIAL, EXIT_REFUSED_BY_STAGE = 3, 4
STOP_CODES = ("budget_exhausted", "rate_limited")
PEOPLE_SKIP_CODES = ("looked_up", "no_evidence", "suppressed", "not_attempted")
SHEET_REFUSAL_CODES = ("header_mismatch", "header_missing")
DRY_RUN_DOMAIN = "example.invalid"
DRY_RUN_EVIDENCE = Evidence(
    DRY_RUN_DOMAIN,
    "The public repository example-org/examplelib-tools matches the term examplelib, "
    "last pushed on 2026-09-30.",
    "https://example.invalid/example-org/examplelib-tools", "repo")
SYNTHETIC_SENDER = Sender("Sample Sender", "Engineer", "Example Org",
                          "1 Example Street, Example City",
                          "Reply stop and you will not hear from us again.")
_EMAIL_DRAFT = COLUMNS.index("email_draft")
_EMAIL = COLUMNS.index("email")
_DROP_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")


def build_live_provider(name, budget, registry):
    """(provider, charges_its_own_budget). Hunter and Apollo charge the budget per request."""
    if name == "hunter":
        return HunterProvider(budget, dry_run=False), True
    if name == "apollo":
        return ApolloProvider(budget, dry_run=False), True
    return registry.get(name), False


def _domain_suppressed(suppression, domain):
    if suppression is None:
        return False
    if suppression.matches(SimpleNamespace(email=None, company_domain=domain)):
        return True
    email_domains = {e.rpartition("@")[2] for e in suppression.emails}
    return any(domain == d or domain.endswith("." + d) for d in email_domains)


def _dropped_line(domain, provider):
    """``people dropped for DOMAIN: code n, ...`` from ``provider.last_dropped``, or None.

    Code-only: a key that is not a lowercase code, or a count that is not a positive whole
    number, is ignored, so no name, title or email can reach the line.
    """
    dropped = getattr(provider, "last_dropped", None)
    if not isinstance(dropped, dict):
        return None
    parts = [f"{code} {n}" for code, n in sorted(dropped.items(), key=str)
             if isinstance(code, str) and _DROP_CODE_RE.fullmatch(code)
             and isinstance(n, int) and not isinstance(n, bool) and n > 0]
    return f"people dropped for {domain}: {', '.join(parts)}" if parts else None


def people_stage(evidence_by_domain, provider, self_charging, budget, titles, suppression, *,
                 out=None):
    """((person, evidence) list, Counter of codes, stop code or None). Never raises.

    After each successful lookup, a provider's ``last_dropped`` counts are printed as one
    code-only line. A lookup that raised prints none.
    """
    people, counts, stop = [], Counter(), None
    for domain, items in evidence_by_domain.items():
        if not items:
            counts["no_evidence"] += 1
            continue
        if _domain_suppressed(suppression, domain):
            counts["suppressed"] += 1
            continue
        if stop is not None:
            counts["not_attempted"] += 1
            continue
        try:
            if self_charging:
                found = provider.find_people(domain, titles, PEOPLE_LIMIT)
            else:
                found = find_people_within(budget, provider, domain, titles, PEOPLE_LIMIT)
        except BudgetExceeded:
            stop = "budget_exhausted"
            counts[stop] += 1
            continue
        except ProviderError as e:
            counts[e.code] += 1
            if e.code in STOP_CODES:
                stop = e.code
            continue
        except Exception:  # a provider problem is this domain's outcome, never an abort
            counts["provider_error"] += 1
            continue
        if not isinstance(found, list) or not all(isinstance(p, Person) for p in found):
            counts["bad_response"] += 1
            continue
        counts["looked_up"] += 1
        people += [(p, items) for p in found[:PEOPLE_LIMIT]]
        line = _dropped_line(domain, provider)
        if line is not None:
            print(line, file=out if out is not None else sys.stdout)
    return people, counts, stop


def draft_stage(people, sender, suppression):
    """(records for the sheet, Counter of codes). Suppressed people get no draft and no row."""
    guarded = GuardedDrafter(TemplateDrafter())
    records, counts = [], Counter()
    for person, items in people:
        result = draft_for(person, items, guarded, sender, suppression)
        if result.status == "suppressed":
            counts["suppressed"] += 1
            continue
        evidence = next((e for e in items
                         if e.company_domain.lower() == person.company_domain.lower()), items[0])
        signal_evidence = f"{evidence.summary} {evidence.url}"
        if result.status == "drafted":
            counts["drafted"] += 1
            text = f"Subject: {result.draft.subject}\n\n{result.draft.body}"
            records.append((person, signal_evidence, text))
        else:
            counts[result.code] += 1
            records.append((person, signal_evidence, ""))
    return records, counts


def _counts(counts, skip=()):
    return ", ".join(f"{code} {n}" for code, n in sorted(counts.items()) if code not in skip)


def _print_stage_counts(out, people_counts, people, draft_counts):
    print(f"people: {people_counts.get('looked_up', 0)} domain(s) looked up, {len(people)} "
          "people found", file=out)
    skipped = _counts(people_counts, skip=("looked_up",))
    if skipped:
        print(f"people skipped: {skipped}", file=out)
    print(f"drafts: {draft_counts.get('drafted', 0)} drafted", file=out)
    skipped = _counts(draft_counts, skip=("drafted",))
    if skipped:
        print(f"drafts skipped: {skipped}", file=out)


def _display(value, width=60):
    text = "".join(c if c.isprintable() else " " for c in str(value))
    return text if len(text) <= width else text[:width - 3] + "..."


def sheet_preflight(writer):
    """One header read through the writer's own client, before any GitHub or provider call.

    The token it fetches is cached by that client, so the write reuses it: the preflight costs
    one sheets call in total. It reads only the first row and writes nothing. An empty first row
    (an empty sheet) or the tool's own header passes; another header raises header_mismatch, and
    a token or read failure raises its own code. The writer still checks the header itself.
    """
    header = writer._live_client().read_header()
    if header and header[:len(COLUMNS)] != list(COLUMNS):
        raise ProviderError("header_mismatch")
    return "the tool's header" if header else "empty first row"


def _dry_run(environ, out, err, run_id):
    try:
        settings = config.load_settings(environ)
        sender = sender_from_env(environ)
        suppression = suppression_from_env(environ)
    except config.ConfigError as e:
        print(f"run: refused: {e}", file=err)
        return EXIT_REFUSED_BEFORE_START
    except DraftError as e:
        print(f"run: refused: {e}", file=err)
        return EXIT_REFUSED_BEFORE_START
    if not all(getattr(sender, f) for f in ("name", "role", "org_name", "postal_address",
                                             "optout_text")):
        sender, sender_note = SYNTHETIC_SENDER, "synthetic placeholder (set the five sender " \
                                                "variables before a live run)"
    else:
        sender_note = "from the environment"
    print("run: dry run (no network, nothing written)", file=out)
    print(f"collect: synthetic, 1 evidence item on {DRY_RUN_DOMAIN} (no GitHub request)", file=out)
    print(f"sender: {sender_note}", file=out)
    provider_budget = CallBudget(settings.provider_max_calls)
    people, people_counts, _ = people_stage({DRY_RUN_DOMAIN: (DRY_RUN_EVIDENCE,)}, FakeProvider(),
                                            False, provider_budget, (), suppression)
    records, draft_counts = draft_stage(people, sender, suppression)
    _print_stage_counts(out, people_counts, people, draft_counts)
    print(f"sheet: would append {len(records)} row(s) (the sheet is not read in a dry run)",
          file=out)
    print("columns: " + " | ".join(COLUMNS), file=out)
    sample = next((r for r in records if r[2]), records[0] if records else None)
    if sample is not None:
        person, signal_evidence, text = sample
        row = SheetWriter(Settings(), run_id=run_id).build_row(person, signal_evidence,
                                                              "(written at run time)", text)
        shown = list(row)
        masked = mask_email(person.email or "")
        shown[_EMAIL] = masked
        shown[_EMAIL_DRAFT] = f"(draft, {len(text)} characters)" if text else ""
        print("sample row: " + " | ".join(_display(c) for c in shown), file=out)
        if text:
            print("sample draft:", file=out)
            print(f"To: {masked or '(no email)'}", file=out)
            print(text.replace(person.email, masked) if person.email else text, file=out)
    print(f"requests made: github 0, provider {provider_budget.used} of "
          f"{settings.provider_max_calls}, sheets 0", file=out)
    print("run: dry run complete; nothing written, nothing sent", file=out)
    return EXIT_COMPLETE


def _live(environ, stdin, out, err, registry, github_http, provider_factory, sheets_http,
          signer, today, clock, run_id):
    gates = check_gates(environ, registry, signer=signer)
    if gates.failures:
        print(f"run: refused: failing gates: {', '.join(gates.failures)}; nothing requested",
              file=err)
        return EXIT_REFUSED_BEFORE_START
    if not (_is_terminal(stdin) and _is_terminal(out)):
        print("run: refused: stdin and stdout must be a terminal (run --live needs a typed "
              "confirmation); nothing requested", file=err)
        return EXIT_REFUSED_BEFORE_START
    settings, signal, plan = gates.settings, gates.signal, gates.plan
    print("run: live plan", file=out)
    print(f"signal: {signal.name}", file=out)
    print(f"targets: {len(signal.targets)} ({plan.targets_planned} planned, "
          f"{len(plan.skipped)} skipped as suppressed)", file=out)
    print(f"terms: {len(signal.terms)}", file=out)
    print(f"max calls: github {plan.total} of {settings.max_calls}; provider at most "
          f"{plan.targets_planned} of {settings.provider_max_calls}; sheets "
          f"{SHEETS_CALLS_PER_WRITE} for the header check and one append, plus 1 per extra "
          f"append as needed, at most {settings.sheets_max_calls}; search ceiling "
          f"{SEARCH_RATE_CEILING_PER_MINUTE} per minute (unverified)", file=out)
    print(f"provider: {settings.provider}", file=out)
    if settings.provider == "hunter" and signal.titles:
        print(HUNTER_TITLES_WARNING, file=out)
    print(f"sheet: {settings.sheet_id[:4]}... (masked)", file=out)
    print("nothing is ever sent by email", file=out)
    print(f'Type "{CONFIRM_WORD}" to run: ', end="", file=out)
    out.flush()
    try:
        answer = stdin.readline()
    except (OSError, ValueError, KeyboardInterrupt):
        answer = ""
    if answer.rstrip("\n") != CONFIRM_WORD:
        print("run: not confirmed; nothing requested", file=err)
        return EXIT_DECLINED

    github_budget = CallBudget(settings.max_calls)
    provider_budget = CallBudget(settings.provider_max_calls)
    sheets_budget = CallBudget(settings.sheets_max_calls)
    partial, refused = [], None

    writer = SheetWriter(Settings(dry_run=False, sheet_id=settings.sheet_id,
                                  google_credentials=settings.google_credentials),
                         budget=sheets_budget, http=sheets_http, signer=signer, out=out,
                         clock=clock, run_id=run_id)
    try:
        found = sheet_preflight(writer)
    except ProviderError as e:
        print(f"sheet preflight: refused ({e.code}); nothing collected, no people looked up",
              file=out)
        print(f"requests made: github 0 of {settings.max_calls}, provider 0 of "
              f"{settings.provider_max_calls}, sheets {sheets_budget.used} of "
              f"{settings.sheets_max_calls}", file=out)
        print(f"run: refused (sheet preflight: {e.code})", file=out)
        return EXIT_REFUSED_BY_STAGE
    print(f"sheet preflight: ok ({found})", file=out)

    client = GitHubClient(github_http, github_budget, settings.github_token, dry_run=False)
    result = collect(signal, client, gates.suppression, settings.max_calls,
                     today if today is not None else utc_today())
    for line in summarize(result):
        print(line, file=out)
    for domain, items in result.evidence_by_domain.items():
        for evidence in items:
            print(f"evidence for {domain}:", file=out)
            print(f"  summary: {evidence.summary}", file=out)
            print(f"  url: {evidence.url}", file=out)
    status = result.run_report.status
    if status == REFUSED:
        refused = f"collect: {result.run_report.code}"
    elif status == PARTIAL:
        partial.append(f"collect: {result.run_report.code or 'partial'}")

    records = []
    if refused is None:
        factory = provider_factory if provider_factory is not None else build_live_provider
        provider, self_charging = factory(settings.provider, provider_budget, registry)
        people, people_counts, stop = people_stage(result.evidence_by_domain, provider,
                                                   self_charging, provider_budget, signal.titles,
                                                   gates.suppression, out=out)
        failures = sorted(c for c in people_counts if c not in PEOPLE_SKIP_CODES)
        if stop is not None:
            partial.append(f"people: {stop}")
        elif failures:
            partial.append(f"people: {failures[0]}")
        records, draft_counts = draft_stage(people, gates.sender, gates.suppression)
        _print_stage_counts(out, people_counts, people, draft_counts)

    if refused is None and not records:
        print("sheet: nothing to write", file=out)
    elif refused is None:
        try:
            writer.write(records)   # the preflight's writer: same client, token reused
        except ProviderError as e:
            if e.code in SHEET_REFUSAL_CODES:
                refused = f"sheet: {e.code}"
            else:
                partial.append(f"sheet: {e.code}")
            if isinstance(e, AppendStopped):
                print(f"sheet: stopped ({e.code}): {e.written} row(s) written, "
                      f"{e.not_written} not written", file=out)
            else:
                print(f"sheet: not written ({e.code})", file=out)

    print(f"requests made: github {github_budget.used} of {settings.max_calls}, provider "
          f"{provider_budget.used} of {settings.provider_max_calls}, sheets "
          f"{sheets_budget.used} of {settings.sheets_max_calls}", file=out)
    if refused is not None:
        print(f"run: refused ({refused})", file=out)
        return EXIT_REFUSED_BY_STAGE
    if partial:
        print(f"run: partial ({'; '.join(partial)})", file=out)
        return EXIT_PARTIAL
    print("run: complete; nothing sent by email", file=out)
    return EXIT_COMPLETE


def run(live=False, environ=None, stdin=None, out=None, err=None, *, registry=None,
        github_http=None, provider_factory=None, sheets_http=None, signer=None, today=None,
        clock=None, run_id=None) -> int:
    """The ``repo-signals run`` command. The keyword arguments are test seams."""
    environ = os.environ if environ is None else environ
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    registry = registry if registry is not None else default_registry()
    run_id = run_id if run_id is not None else secrets.token_hex(8)
    if not live:
        return _dry_run(environ, out, err, run_id)
    return _live(environ, stdin, out, err, registry, github_http, provider_factory, sheets_http,
                 signer, today, clock, run_id)
