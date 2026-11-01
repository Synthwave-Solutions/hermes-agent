"""Auth lock diagnostics.

Dependencies on the runtime facade are resolved at call time.
"""
from __future__ import annotations

from pathlib import Path

def _lock_holder_hint(lock_path: Path) -> str:
    """Live-holder hint from the pid a holder stamps into the lock file; empty when there is none.

    Holders stamp their pid on acquire and clear it on release, so a leftover pid from a dead
    process is filtered by a liveness probe — stay silent instead of blaming a ghost."""
    from hermes_cli.auth import (
        os
    )
    try:
        first_token = lock_path.read_text(encoding="utf-8-sig", errors="replace").split()[0]
        pid = int(first_token)
    except (OSError, ValueError, IndexError):
        return ""
    if pid <= 0 or pid == os.getpid():
        return ""
    if os.name == "posix":
        try:
            os.kill(pid, 0)  # windows-footgun: ok — inside `if os.name == "posix"` gate
        except ProcessLookupError:
            return ""
        except OSError:
            pass  # exists but is not signalable (e.g. EPERM): still a live holder
    # No example holder: many stores use this lock; the caller's timeout_message names which one.
    return f"another hermes process (pid {pid}) probably still holds it"
