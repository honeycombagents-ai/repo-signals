"""``repo-signals init [--dir PATH]``: offline guided setup that writes starter files.

It never asks for, reads or stores a token, API key, password or the contents of a credentials
file. It prints the doctor checklist (variable names only), shows the files it would create, and
writes them only after a typed "yes" at a terminal. There is no flag to skip that.

Files are only ever created, never overwritten: if any target exists, nothing is written. The
directory is created with mode 0700 and each file with mode 0600, and a ``.gitignore`` holding
``*`` keeps the folder out of git. It refuses to write inside a git work tree or over a file.
It also refuses a target path whose final component is a symlink; only that final component is
checked, and symlinks earlier in the path are resolved.
"""
import os
import shutil
import stat
import sys
from pathlib import Path

from . import config, doctor
from .providers.registry import default_registry

DEFAULT_DIR = "repo-signals-config"
CONFIRM_WORD = "yes"
EXIT_WRITTEN, EXIT_DECLINED, EXIT_REFUSED = 0, 1, 2
DIR_MODE, FILE_MODE = 0o700, 0o600

SIGNAL_TOML = '''\
# Repo Signals signal file. Replace the example term and target with your own, then check it:
#   repo-signals collect --dry-run
# Only example.com appears here. See docs/signal-file.md for every field.
name = "example-signal"
terms = ["examplelib"]
pushed_within_days = 365
# language = "Python"
# titles = ["CTO", "VP Engineering"]

[[targets]]
domain = "example.com"
org = "example-org"
'''

SUPPRESSION_TXT = '''\
# Suppression list: one email address or one domain per line.
# Lines starting with # are ignored. Nobody listed here gets a draft or a people lookup.
'''

GITIGNORE = "*\n"

VARIABLE_NOTES = {
    config.PROVIDER: "people provider: fake, hunter or apollo",
    config.MAX_CALLS: "GitHub call budget, 1 to 500",
    config.PROVIDER_MAX_CALLS: "people-provider call budget, 1 to 500",
    config.SHEETS_MAX_CALLS: "Google Sheets call budget, 1 to 500",
    config.SHEET_ID: "the Google Sheet id",
    config.GITHUB_TOKEN: "secret, optional: set it with read -rs",
    config.GOOGLE_CREDENTIALS: "path to the service-account key file, kept outside any repository",
    config.SUPPRESSION_FILE: "path to suppression.txt",
    config.SIGNAL_FILE: "path to signal.toml",
    config.DRY_RUN: "true by default; exactly false allows live commands",
}


def all_variable_names(registry=None):
    """Every REPO_SIGNALS_* setting in config, then every provider's own variables."""
    registry = registry if registry is not None else default_registry()
    names = [v for k, v in vars(config).items()
             if k.isupper() and isinstance(v, str) and v.startswith("REPO_SIGNALS_")]
    names = list(dict.fromkeys(names))
    for provider_name in registry.names():
        for var in registry.get(provider_name).required_env:
            if var not in names:
                names.append(var)
    return names


def env_example(registry=None):
    lines = ["# Repo Signals variable NAMES only. Never write a value in this file.",
             "# Set each one in your shell. For secrets, read the value without echoing it:",
             "#   read -rs NAME; export NAME",
             "# Then check with: repo-signals doctor", ""]
    for name in all_variable_names(registry):
        note = VARIABLE_NOTES.get(name)
        if note is None and name in config.SENDER_VARS:
            note = "sender detail, see sender.example"
        if note is None:
            note = "secret for a people provider: set it with read -rs"
        lines += [f"# {note}", name, ""]
    return "\n".join(lines)


def sender_example():
    lines = ["# The five sender variables every draft needs (see docs/drafting.md).",
             "# Names only. Set them in your shell; the postal address and opt-out text may span",
             "# up to 6 lines each.", ""]
    return "\n".join(lines + list(config.SENDER_VARS)) + "\n"


def planned_files(registry=None):
    return {"signal.toml": SIGNAL_TOML, "env.example": env_example(registry),
            "sender.example": sender_example(), "suppression.txt": SUPPRESSION_TXT,
            ".gitignore": GITIGNORE}


def _is_terminal(stream):
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except (OSError, ValueError):
        return False


def _inside_git_work_tree(path):
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return True
    return False


def check_target(directory, cwd=None):
    """(resolved Path, None) or (None, refusal code). Reads no file contents."""
    base = Path(cwd) if cwd is not None else Path.cwd()
    raw = Path(directory) if directory else Path(DEFAULT_DIR)
    path = raw if raw.is_absolute() else base / raw
    if path.is_symlink():
        return None, "path_is_a_symlink"
    resolved = path.resolve()
    if resolved.exists() and not resolved.is_dir():
        return None, "path_is_a_file"
    if _inside_git_work_tree(resolved):
        return None, "inside_a_git_work_tree"
    if resolved.is_dir():
        mode = stat.S_IMODE(os.lstat(resolved).st_mode)
        if mode & 0o077:
            return None, "directory_open_to_other_users"
    return resolved, None


def _write_new(path, content):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        data = content.encode("utf-8")
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


def run_init(directory=None, environ=None, stdin=None, out=None, err=None, cwd=None,
             registry=None, which=None) -> int:
    environ = os.environ if environ is None else environ
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    registry = registry if registry is not None else default_registry()

    print("init: checklist (variable names only, no values)", file=out)
    for status, message in doctor.run_checks(environ, registry,
                                              which=which if which is not None else shutil.which):
        print(f"  {status} {message}", file=out)

    resolved, problem = check_target(directory, cwd)
    if problem:
        print(f"init: refused: {problem}; nothing written", file=err)
        return EXIT_REFUSED
    files = planned_files(registry)
    existing = [name for name in files if os.path.lexists(resolved / name)]
    if existing:
        print(f"init: refused: target_exists ({', '.join(existing)}); nothing written", file=err)
        return EXIT_REFUSED
    print(f"init: would create in {resolved}:", file=out)
    for name in files:
        print(f"  {name}", file=out)
    print("  (directory mode 0700, files mode 0600; nothing is ever overwritten)", file=out)

    if not (_is_terminal(stdin) and _is_terminal(out)):
        print("init: refused: stdin and stdout must be a terminal; nothing written", file=err)
        return EXIT_REFUSED
    print(f'Type "{CONFIRM_WORD}" to create these files: ', end="", file=out)
    out.flush()
    try:
        answer = stdin.readline()
    except (OSError, ValueError, KeyboardInterrupt):
        answer = ""
    if answer.rstrip("\n") != CONFIRM_WORD:
        print("init: not confirmed; nothing written", file=err)
        return EXIT_DECLINED

    try:
        if not resolved.exists():
            resolved.mkdir(mode=DIR_MODE, parents=True)
            os.chmod(resolved, DIR_MODE)
        for name, content in files.items():
            _write_new(resolved / name, content)
    except OSError:
        print("init: could not create every file (nothing is overwritten); check the directory",
              file=err)
        return EXIT_REFUSED

    print(f"init: created {len(files)} files in {resolved}", file=out)
    print("next steps:", file=out)
    print("  1. Export each variable in env.example in your shell. For a secret, use", file=out)
    print("     read -rs NAME; export NAME", file=out)
    print("     Never write a secret into a file inside a repository.", file=out)
    print("  2. repo-signals doctor", file=out)
    print("  3. repo-signals run            (the dry run: no network, nothing written)", file=out)
    print("  4. The first live run: one target, one term, tiny budgets, a throwaway sheet,", file=out)
    print("     then repo-signals run --live", file=out)
    return EXIT_WRITTEN
