"""The mod manager's one error type, plus the non-fatal warn channel."""
import sys


class ModManagerError(Exception):
    """One error type the UI can catch and show verbatim. Every raise below
    carries a specific, actionable message (which mods, which versions, which
    repo) rather than a generic failure."""


def _warn(msg):
    """Non-fatal problems (a skipped unknown-major manifest, an unreachable
    third-party repo) print to stderr rather than raising, so one bad source
    doesn't break the others. Kept tiny on purpose."""
    try:
        print(f"[modmanager] {msg}", file=sys.stderr)
    except Exception:
        pass
