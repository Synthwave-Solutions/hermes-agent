#!/usr/bin/env python3
"""profile_scope - per-profile project/folder scoping for Hermes.

Confines a SCOPED profile (e.g. a freelancer like "steve") to a fixed set of
project folders plus safe scratch roots, and hard-denies any reference to
secrets, other profiles, or system credentials. Profiles NOT listed as scoped
are entirely unaffected (fail-open), so the existing workstation profiles keep
working exactly as before.

Who is scoped and to which folders is configuration, not code: see
``~/.hermes/profile-scopes.yaml`` (override with $HERMES_PROFILE_SCOPES_FILE).
The file is re-read whenever its mtime changes, so a scope edit takes effect on
the next tool call without restarting any gateway. When the file is absent the
module falls back to the built-in map below, so removing it changes nothing.

Fail modes, on purpose:
  - no config file          -> built-in fallback map (steve, marcel)
  - config file unreadable  -> DEGRADED: every profile carrying the marker file
                               stays scoped but keeps only its safe roots, so a
                               broken edit locks down rather than opens up
  - profile not listed      -> fail-open, unchanged behaviour

Threat model (stated honestly): the active profile shares one unix user with
everything else on the box, so a path-allowlist over arbitrary shell is
best-effort, not airtight against a determined insider. This layer is built to
(a) enforce project boundaries and (b) block casual / accidental access to
secrets and other profiles. The real cred boundary is creds-by-absence: a
scoped profile's gateway is launched with a scrubbed env and an empty .env, so
its agent never holds the crown-jewel keys in the first place.

Stdlib-only so it can be unit-tested offline and imported by the approval gate
(terminal commands) and the file tools. PyYAML is used when present; a JSON
config file works without it.

resolve_profile() -> str            active profile name (from HERMES_HOME)
is_scoped(profile) -> bool          is this profile under enforcement
check_command(profile, command, cwd=None) -> (allowed: bool, reason: str)
check_path(profile, path, mode="read") -> (allowed: bool, reason: str)
allowed_roots(profile) -> list[str] resolved absolute project roots
config_status() -> dict             which source is live, and any parse error
"""
from __future__ import annotations

import json
import os
import re
import sys

# --- configuration -----------------------------------------------------------

# Where the editable scope map lives. Kept next to the module so one backup
# covers both.
CONFIG_FILE = "~/.hermes/profile-scopes.yaml"

# Marker filename dropped in each scoped profile's home dir. The tools-side
# bridge (hermes-agent tools/profile_scope_bridge.py) uses it to recognise a
# scoped home even when this module fails to load, so guard errors can still
# fail closed for scoped profiles only (and fail open for everyone else).
SCOPED_PROFILE_MARKER = ".profile-scoped"

# Built-in fallback, used only when no config file exists. Mirrors the state
# before the map became configurable, so an absent file is a no-op.
_FALLBACK_SCOPED = {"steve", "marcel"}
_FALLBACK_PROJECT_ROOTS = {
    "steve": [
        "~/synthwave-github-projects/Synthwave-Solutions/ceo-compass-backend",
        "~/synthwave-github-projects/Synthwave-Solutions/ceo-compass-frontend",
        "~/synthwave-github-projects/Synthwave-Solutions/ceo-compass-frontend-landing",
        "~/clients/alta-ict",
        "~/clients/360kas",
        "~/clients/ivcb",
        "~/clients/adams",
        "~/clients/iccxx",
        "~/clients/johnson-controls",
        "~/synthwave-github-projects/Synthwave-Solutions/opendesign-projects/ett-institute-quickscan-rapport",
    ],
    # Marcel's functional integration boundary is Attio. His powerful shell
    # runs in Docker, where the profile workspace is mounted at /workspace and
    # the host filesystem is not mounted.
    "marcel": ["/workspace"],
}

# Hard-deny roots: secrets, other profiles, system creds. Any command or path
# that references one of these is blocked for a scoped profile, even if it sits
# inside an otherwise-allowed parent. Checked as a prefix on the normalised
# absolute path.
_DENY_ROOTS = [
    "~/.hermes/.env",
    "~/.hermes/secrets",
    "~/.hermes/auth.json",
    "~/.hermes/config.yaml",
    "~/.hermes/gmail-dwd-sa.json",
    "~/.hermes/google_token.json",
    "~/.hermes/google_chat_token.json",
    "~/.hermes/google_client_secret.json",
    "~/.hermes/google_oauth_pending.json",
    "~/.hermes/google_chat_oauth_pending.json",
    "~/.hermes/impersonation-audit.log",
    "~/.hermes/identity_tiers.py",
    "~/.hermes/profile_scope.py",
    "~/.hermes/profile-scopes.yaml",
    "~/.ssh",
    "~/.aws",
    "~/.netrc",
    "~/.config/gcloud",
    "~/.config/synthwave",
    "~/.npmrc",
    "~/.git-credentials",
]


def _real_home() -> str:
    """The account's passwd home, independent of the HOME env var. Gateways
    that override HOME (the profile multiplexer runs with HOME under
    ~/.hermes-mux) must still anchor every scope root at the real ~."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser("~")


def _expand(p: str) -> str:
    """expanduser, but '~' means the passwd home, never the HOME env var."""
    if p == "~" or p.startswith("~/"):
        return _real_home() + p[1:]
    return os.path.expanduser(p)


def _norm(p: str) -> str:
    """Expand ~, env vars, and resolve . / .. to an absolute path (no symlink
    resolution - we operate on the textual path the command will use)."""
    p = _expand(os.path.expandvars(p.strip()))
    if not os.path.isabs(p):
        p = os.path.abspath(p)
    return os.path.normpath(p)


def _variants(np: str) -> tuple:
    """The textual path plus its symlink-resolved twin. Matching both keeps
    the textual threat model AND covers the multiplexer's symlinked
    profiles tree (~/.hermes-mux/profiles/<name> -> ~/.hermes/profiles/<name>).
    Realpath strengthens deny (a symlink into secrets is caught) and only
    relaxes allow toward targets that are themselves allowed."""
    try:
        rp = os.path.realpath(np)
    except OSError:
        return (np,)
    return (np,) if rp == np else (np, rp)


def _norm_all(paths) -> list[str]:
    return [_norm(p) for p in paths]


# --- config loading ----------------------------------------------------------

# Cache keyed on (path, mtime, size) so an edit is picked up mid-process and a
# rewrite of the same length still invalidates.
_cache: dict = {"key": None, "scoped": None, "roots": None, "deny": None,
                "source": "fallback", "error": ""}


def _config_path() -> str:
    return _norm(os.environ.get("HERMES_PROFILE_SCOPES_FILE") or CONFIG_FILE)


def _marker_profiles() -> set:
    """Profiles carrying the marker file. Used for the degraded fail-closed
    path, where the config cannot be trusted but we still know who is armed."""
    found = set()
    proot = _profiles_root()
    try:
        for name in os.listdir(proot):
            if os.path.exists(os.path.join(proot, name, SCOPED_PROFILE_MARKER)):
                found.add(name)
    except OSError:
        pass
    return found


def _parse(text: str) -> dict:
    """Parse the config as YAML when PyYAML is available, else as JSON."""
    try:
        import yaml  # type: ignore
    except ImportError:
        return json.loads(text)
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("scope config must be a mapping")
    return data


def _build(data: dict) -> tuple:
    """Turn parsed config into (scoped set, roots map, extra-deny map)."""
    projects = data.get("projects") or {}
    profiles = data.get("profiles") or {}
    if not isinstance(projects, dict) or not isinstance(profiles, dict):
        raise ValueError("'projects' and 'profiles' must be mappings")

    scoped, roots, deny = set(), {}, {}
    for name, spec in profiles.items():
        spec = spec or {}
        if not isinstance(spec, dict):
            raise ValueError(f"profile {name!r} must be a mapping")
        if spec.get("enforce") is False:
            continue  # listed but parked: stays fail-open, keeps its folders
        paths: list = []
        for proj in spec.get("projects") or []:
            if proj not in projects:
                raise ValueError(f"profile {name!r} refers to unknown project {proj!r}")
            paths.extend(projects[proj] or [])
        paths.extend(spec.get("roots") or [])
        scoped.add(name)
        roots[name] = paths
        deny[name] = list(spec.get("deny") or [])
    return scoped, roots, deny


def _load() -> tuple:
    """(scoped, roots, deny). Cached on the config file's mtime."""
    path = _config_path()
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, None, None)

    if _cache["key"] == key:
        return _cache["scoped"], _cache["roots"], _cache["deny"]

    if key[1] is None:  # no config file: built-in fallback, nothing changes
        _cache.update(key=key, scoped=set(_FALLBACK_SCOPED),
                      roots=dict(_FALLBACK_PROJECT_ROOTS), deny={},
                      source="fallback", error="")
        return _cache["scoped"], _cache["roots"], _cache["deny"]

    try:
        scoped, roots, deny = _build(_parse(open(path, encoding="utf-8").read()))
        _cache.update(key=key, scoped=scoped, roots=roots, deny=deny,
                      source="config", error="")
    except Exception as exc:  # noqa: BLE001 - any parse problem locks down
        armed = _marker_profiles()
        _cache.update(key=key, scoped=armed, roots={p: [] for p in armed},
                      deny={}, source="degraded",
                      error=f"{type(exc).__name__}: {exc}")
    return _cache["scoped"], _cache["roots"], _cache["deny"]


def config_status() -> dict:
    """Where the live scope map came from, for the CLI and health checks."""
    _load()
    return {"path": _config_path(), "source": _cache["source"],
            "error": _cache["error"], "profiles": sorted(_cache["scoped"])}


def scoped_profiles() -> frozenset:
    """Frozen copy of the enforced profile set (for the tools-side bridge)."""
    scoped, _, _ = _load()
    return frozenset(scoped)


# Scratch / own-profile roots always allowed for a scoped profile.
def _safe_roots(profile: str) -> list[str]:
    return [
        f"~/.hermes/profiles/{profile}",   # the profile's own home/workspace
        "/tmp",
        "/var/tmp",
    ]


def allowed_roots(profile: str) -> list[str]:
    _, roots, _ = _load()
    return _norm_all(list(roots.get(profile, [])) + _safe_roots(profile))


def _deny_roots(profile: str | None = None) -> list[str]:
    _, _, deny = _load()
    extra = list(deny.get(profile, [])) if profile else []
    return _norm_all(_DENY_ROOTS + extra)


# Hard-deny on other profiles' homes (anything under profiles/ that is not the
# active profile's own dir).
def _profiles_root() -> str:
    return _norm("~/.hermes/profiles")


def is_scoped(profile: str | None) -> bool:
    scoped, _, _ = _load()
    return (profile or "") in scoped


def _active_home() -> str:
    """The Hermes home for THIS turn.

    Under the profile multiplexer one process serves every profile, and the
    per-turn home is a contextvar set by the gateway, not the HERMES_HOME env
    var (which stays pinned at the multiplexer's own root). Ask the agent for
    the override first and only then fall back to the environment, otherwise
    every multiplexed request resolves to the multiplexer instead of to the
    caller's profile.
    """
    try:
        from hermes_constants import get_hermes_home_override
        override = get_hermes_home_override()
        if override:
            return str(override)
    except Exception:
        pass
    return os.environ.get("HERMES_HOME") or ""


def resolve_profile() -> str:
    """Active profile name, inferred from the active Hermes home. 'default'
    when unset or pointing at ~/.hermes; the profile dir name when under
    profiles/<name>, including through the multiplexer's symlinked tree."""
    home = _active_home()
    if not home:
        return "default"
    home_n = _norm(home)
    if home_n == _norm("~/.hermes"):
        return "default"
    proot = _profiles_root()
    for cand in _variants(home_n):
        if cand.startswith(proot + os.sep):
            rest = cand[len(proot) + 1:].split(os.sep)
            if rest and rest[0]:
                return rest[0]
    return "custom"


def _is_under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def check_path(profile: str, path: str, mode: str = "read") -> tuple[bool, str]:
    """Allow/deny a single explicit filesystem path for a scoped profile.

    Reliable for file tools (the path is explicit). For non-scoped profiles
    always allows.
    """
    if not is_scoped(profile):
        return True, "profile not scoped"
    np = _norm(path)
    cands = _variants(np)

    # Hard-deny secrets / system creds first.
    for d in _deny_roots(profile):
        if any(_is_under(c, d) for c in cands):
            return False, f"denied: {path} is a protected secret/credential path"

    # Deny other profiles' homes (own profile dir is in allowed roots below).
    proot = _profiles_root()
    own = _norm(f"~/.hermes/profiles/{profile}")
    if (any(_is_under(c, proot) for c in cands)
            and not any(_is_under(c, own) for c in cands)):
        return False, f"denied: {path} belongs to another profile"

    # Allow if inside any allowed root.
    for r in allowed_roots(profile):
        if any(_is_under(c, r) for c in cands):
            return True, f"allowed: under {r}"

    return False, (f"out of scope: {path} is not under any of "
                   f"{profile}'s allowed project roots")


# Tokens in a shell command that look like filesystem paths. We extract
# candidate paths and deny if any hits a deny-root or lands clearly outside
# scope. Captures ~, absolute, ./ and ../ relative, and $VAR / ${VAR}
# prefixed tokens (expanded by _norm). This is heuristic (see threat model
# in the module docstring).
_PATH_TOKEN_RE = re.compile(
    r"""(?:^|[\s=:'"(])((?:~|/|\.\.?/|\$\{?[A-Za-z_])[^\s'"();|&<>]*)""")


def check_command(profile: str, command: str, cwd: str | None = None
                  ) -> tuple[bool, str]:
    """Best-effort allow/deny of a shell command for a scoped profile.

    Strategy:
      1. Non-scoped profile  -> always allow (fail-open, workstation untouched).
      2. Any reference to a deny-root (secrets / other profile / creds) -> deny.
      3. Otherwise allow; explicit path tokens that point clearly OUTSIDE the
         allowed roots (and are not bare relative names) -> deny.
    cwd, when supplied, is used to resolve relative path tokens.
    """
    if not is_scoped(profile):
        return True, "profile not scoped"

    base = cwd or os.environ.get("PWD") or os.getcwd()

    # 2. Hard-deny any explicit reference to a protected path.
    deny_re = re.compile(
        r"\.hermes/(?:\.env|secrets|auth\.json|config\.yaml|gmail-dwd-sa\.json"
        r"|google_token\.json|google_chat_token\.json"
        r"|google_client_secret\.json|google_oauth_pending\.json"
        r"|google_chat_oauth_pending\.json"
        r"|impersonation-audit\.log|identity_tiers\.py|profile_scope\.py"
        r"|profile-scopes\.yaml)"
        r"|(?<![\w-])\.ssh/|\.aws/|\.netrc|\.config/gcloud|\.config/synthwave"
        r"|\.git-credentials|\.npmrc"
    )
    if deny_re.search(command):
        return False, "denied: command references a protected secret/credential path"

    # Other profiles' homes.
    for m in re.finditer(r"profiles/([A-Za-z0-9_-]+)", command):
        if m.group(1) != profile:
            return False, (f"denied: command references another profile "
                           f"(profiles/{m.group(1)})")

    # 3. Path-token scope check.
    allowed = allowed_roots(profile)
    denies = _deny_roots(profile)
    for m in _PATH_TOKEN_RE.finditer(command):
        tok = m.group(1)
        # Resolve the token. ~, absolute, and $VAR tokens resolve on their
        # own via _norm (an unset $VAR stays literal and abspath-anchors,
        # which safely lands it outside the allowed roots); ./ ../ and bare
        # relative tokens resolve against the tracked cwd.
        if tok.startswith(("~", "/", "$")):
            np = _norm(tok)
        else:
            np = _norm(os.path.join(base, tok))
        cands = _variants(np)
        # deny-root hit
        if any(_is_under(c, d) for d in denies for c in cands):
            return False, f"denied: {tok} is a protected path"
        # Every confidently resolved token (absolute, ~, $VAR, ./ or ../)
        # outside all allowed roots is denied; only bare relative names
        # (e.g. "app.py") are exempt from the allowed-roots check.
        if tok.startswith(("~", "/", "$", "./", "../")):
            if not any(_is_under(c, r) for r in allowed for c in cands):
                return False, (f"out of scope: {tok} is outside "
                               f"{profile}'s allowed project roots")
    return True, "ok"


# --- self-test ---------------------------------------------------------------
if __name__ == "__main__":
    import tempfile

    ok = True

    def _run(label, rows, fn):
        global ok
        for row in rows:
            exp = row[-1]
            got, why = fn(*row[:-1])
            good = got == exp
            ok = ok and good
            print(f"[{'OK ' if good else 'FAIL'}] {label} exp={exp} got={got} "
                  f":: {str(row[1])[:55]} :: {why[:70]}")

    # Part 1: the live map (config file if present, fallback otherwise).
    st = config_status()
    print(f"# live scope map: source={st['source']} profiles={st['profiles']}"
          + (f" error={st['error']}" if st["error"] else ""))

    # The live-map rows need a profile the live map really scopes. They used
    # to name "steve", who was parked (enforce: false) on 14 Sep 2026; every
    # row then read "profile not scoped" and the security gate went red with
    # nothing actually wrong. Test against an armed profile that carries its
    # marker and has no grant covering ~/clients, so a client folder is a
    # real out-of-scope target (Michael Ramirez, 27 Sep 2026).
    def _pick_live_test_profile():
        _, _roots, _ = _load()
        home = _norm("~")
        clients = _norm("~/clients")
        for name in sorted(scoped_profiles()):
            pdir = _norm(f"~/.hermes/profiles/{name}")
            if not os.path.exists(os.path.join(pdir, SCOPED_PROFILE_MARKER)):
                continue
            granted = [r for r in _norm_all(_roots.get(name, []))
                       if _is_under(r, home) and r != home]
            if not granted:
                continue
            if any(_is_under(clients, r) or _is_under(r, clients) for r in granted):
                continue
            return name, granted[0]
        return None, None

    P, IN = _pick_live_test_profile()
    if P is None:
        ok = False
        print("[FAIL] live map: no armed profile with a marker and without a "
              "~/clients grant to test the guard against")
        P, IN = "__no_scoped_profile__", _norm("~/work")
    else:
        print(f"# live test profile: {P} (granted root {IN})")
    IN_VAR = "$HOME" + IN[len(_norm("~")):]
    SUB = os.path.join(IN, "sub")
    _run("cmd", [
        (P, f"ls {IN}", True),                                       # granted root
        (P, f"cat {IN}/README.md", True),
        (P, "echo hi && python app.py", True),                       # relative, allowed
        (P, "cat ~/.hermes/.env", False),                            # secret
        (P, "cat ~/.hermes/secrets/attio.env", False),               # secret
        (P, "cat ~/.hermes/profile-scopes.yaml", False),             # the scope map itself
        (P, "cp ~/.hermes/profiles/financial-ops/.env /tmp/x", False),  # other profile
        (P, "cat ~/clients/heijmans/secret.txt", False),             # out of scope client
        (P, "ls ~/clients/alta-ict", False),                         # client not granted
        (P, "cat ~/.ssh/id_rsa", False),                             # ssh key
        (P, "ls ~/.config/gcloud", False),                           # gcloud creds
        ("financial-ops", "cat ~/.hermes/.env", True),               # non-scoped untouched
        ("default", "cat ~/.hermes/secrets/attio.env", True),        # non-scoped untouched
        (P, "cat ~/.hermes/google_token.json", False),               # google token json
        (P, "cat $HOME/.hermes/google_token.json", False),           # $VAR + token json
        (P, "cp $HOME/clients/heijmans/x /tmp/x", False),            # $VAR out of scope
        (P, f"ls {IN_VAR}", True),                                   # $VAR in scope
        ("default", "cat $HOME/.hermes/google_token.json", True),    # non-scoped untouched
        ("marcel", "printf hello && pwd", True),                     # powerful bash, no path escape
        ("marcel", "cat /workspace/notes.md", True),                 # mounted own workspace
        ("marcel", "cat ~/.hermes/.env", False),                     # shared secret
        ("marcel", "ls ~/clients/alta-ict", False),                  # no shared project tree
    ], check_command)

    _escape_heijmans = os.path.relpath(_norm("~/clients/heijmans/x.txt"), SUB)
    _escape_catapult = os.path.relpath(_norm("~/clients/catapult/x.txt"), IN)
    _run("cwd", [
        (P, f"cat {_escape_heijmans}", SUB, False),
        (P, f"cat {_escape_catapult}", IN, False),
        (P, "cat ../README.md", SUB, True),
        (P, "cat ./README.md", IN, True),
        ("default", f"cat {_escape_heijmans}", SUB, True),
    ], check_command)

    _run("path", [
        (P, os.path.join(IN, "x.py"), "write", True),
        (P, "~/.hermes/secrets/attio.env", "read", False),
        (P, f"~/.hermes/profiles/{P}/workspace/notes.md", "write", True),
        (P, "~/.hermes/profiles/sales-ops/.env", "read", False),
        (P, "~/clients/catapult/x", "read", False),                  # out of scope
        ("default", "~/.hermes/.env", "read", True),
        ("marcel", "~/.hermes/profiles/marcel/workspace/notes.md", "write", True),
        ("marcel", "~/clients/alta-ict/README.md", "read", False),
    ], check_path)

    # Part 2: the config layer itself, against throwaway files.
    _live = os.environ.get("HERMES_PROFILE_SCOPES_FILE")
    with tempfile.TemporaryDirectory() as td:
        cfg = os.path.join(td, "scopes.yaml")
        os.environ["HERMES_PROFILE_SCOPES_FILE"] = cfg

        open(cfg, "w").write(
            "projects:\n"
            "  alta: ['~/clients/alta-ict']\n"
            "  kas:  ['~/clients/360kas']\n"
            "profiles:\n"
            "  tester:\n"
            "    projects: [alta, kas]\n"
            "    roots: ['~/deliverables/tester']\n"
            "    deny:  ['~/clients/alta-ict/geheim']\n"
            "  parked:\n"
            "    projects: [alta]\n"
            "    enforce: false\n"
        )
        _run("cfg", [
            ("tester", "ls ~/clients/alta-ict", True),               # via project
            ("tester", "ls ~/clients/360kas", True),                 # second project
            ("tester", "ls ~/deliverables/tester", True),            # extra root
            ("tester", "ls ~/clients/ivcb", False),                  # not granted
            ("tester", "cat ~/clients/alta-ict/geheim/x", False),    # per-profile deny
            ("parked", "ls ~/clients/ivcb", True),                   # enforce:false = fail-open
            ("steve", "ls ~/clients/alta-ict", True),                # unlisted = fail-open
        ], check_command)

        # An edit is picked up without restarting anything.
        open(cfg, "w").write("projects:\n  ivcb: ['~/clients/ivcb']\n"
                             "profiles:\n  tester:\n    projects: [ivcb]\n")
        _run("reload", [
            ("tester", "ls ~/clients/ivcb", True),
            ("tester", "ls ~/clients/alta-ict", False),
        ], check_command)

        # A broken file locks the armed profiles down instead of opening up.
        open(cfg, "w").write("profiles: [this is not a mapping]\n")
        st = config_status()
        good = st["source"] == "degraded"
        ok = ok and good
        print(f"[{'OK ' if good else 'FAIL'}] degraded exp=degraded "
              f"got={st['source']} :: {st['error'][:60]}")
        _run("degraded", [
            (P, "ls ~/clients/alta-ict", False),         # marker present -> locked down
            (P, "ls /tmp/x", True),                      # safe roots still work
            ("nobody", "ls ~/clients/alta-ict", True),   # no marker -> untouched
        ], check_command)

    if _live is None:
        os.environ.pop("HERMES_PROFILE_SCOPES_FILE", None)
    else:
        os.environ["HERMES_PROFILE_SCOPES_FILE"] = _live

    # Invariant: every scoped profile whose home dir exists must carry the
    # marker file the tools-side bridge uses for fail-closed detection.
    for prof in sorted(scoped_profiles()):
        pdir = _norm(f"~/.hermes/profiles/{prof}")
        if os.path.isdir(pdir):
            marker = os.path.join(pdir, SCOPED_PROFILE_MARKER)
            got = os.path.exists(marker)
            ok = ok and got
            print(f"[{'OK ' if got else 'FAIL'}] marker exp=True got={got} :: {marker}")

    print("ALL PASS" if ok else "SOME FAILED")
    raise SystemExit(0 if ok else 1)
