"""Run one governed OpenCode job and print a review report.

Started by ``tools/opencode_tool.py`` as a background process, never by the
model directly. Everything that decides what OpenCode may touch arrives in the
job file the tool wrote after its governance checks; the model only supplied
the task text.

What this process guarantees (15-10-2026):

* a global limit on concurrent OpenCode jobs (flock slots, default 12);
* OpenCode works directly in the workdir (16-10-2026, no worktree copy); the
  report lists exactly the files this job changed, with their diff;
* OpenCode runs inside bubblewrap: the filesystem is read-only except the
  workdir and OpenCode's own per-job state, and what the caller may not read
  is masked with empty mounts. That holds for OpenCode's bash tool too, which
  a permission file alone cannot promise;
* the OmniRoute key travels in a one-shot file that is deleted before
  OpenCode starts and is masked inside the sandbox;
* the recursion guard is set, so a nested ``opencode run`` is refused.

The report (stdout) is what the completion notification delivers to the agent.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPORT_DIFF_LINES = 400
REPORT_TAIL_CHARS = 6000
SNAPSHOT_FILE_LIMIT = 20000


def _tail(text: str, limit: int = REPORT_TAIL_CHARS) -> str:
    # OpenCode prints its whole permission rule set when it refuses a path;
    # the refusal line matters, the multi-kilobyte rule dump does not.
    text = "\n".join("[permission rules omitted: OpenCode refused an access outside its scope]"
                     if len(line) > 1500 and '"permission"' in line else line
                     for line in (text or "").splitlines())
    return text if len(text) <= limit else "[...]\n" + text[-limit:]


def _run(argv, cwd=None, env=None, timeout=None, input_text=None):
    try:
        # stdin is never inherited: `opencode run` reads a piped stdin as part of
        # the message and would wait for EOF forever.
        extra = {"input": input_text} if input_text is not None else {"stdin": subprocess.DEVNULL}
        done = subprocess.run(argv, cwd=cwd, env=env, timeout=timeout,
                              capture_output=True, text=True, **extra)
        return done.returncode, (done.stdout or "") + (done.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return 124, out + f"\n[timeout after {timeout}s]"
    except OSError as exc:
        return 127, str(exc)


def acquire_slot(slot_dir: Path, slots: int, wait_seconds: int):
    """Hold one of *slots* flock slots; None when all stay busy for wait_seconds."""
    slot_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    deadline = time.monotonic() + max(0, wait_seconds)
    while True:
        for index in range(max(1, slots)):
            handle = open(slot_dir / f"slot-{index}.lock", "w")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                handle.write(str(os.getpid()))
                handle.flush()
                return handle
            except OSError:
                handle.close()
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)


def _git(repo: Path, *args, timeout=120):
    return _run(["git", "-C", str(repo), *args], timeout=timeout)


def _hash(path: Path) -> str:
    try:
        digest = hashlib.sha1()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return ""


def snapshot(root: Path, git_top: str = "") -> dict:
    """Content fingerprints of the files a job could change.

    In a git repo only the dirty and untracked files are hashed (the rest is
    HEAD); elsewhere every file under root, up to SNAPSHOT_FILE_LIMIT."""
    state = {}
    if git_top:
        code, out = _git(Path(root), "status", "--porcelain=v1", "-z", "-uall", "--", ".")
        if code == 0:
            entries = out.split("\0")
            index = 0
            while index < len(entries):
                entry = entries[index]
                index += 1
                if len(entry) < 4:
                    continue
                if entry[0] in "RC":
                    index += 1  # the rename source follows
                rel = entry[3:]
                state[rel] = _hash(Path(git_top) / rel)
            return state
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__"}
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames:
            count += 1
            if count > SNAPSHOT_FILE_LIMIT:
                return state
            full = Path(dirpath) / name
            state[str(full.relative_to(root))] = _hash(full)
    return state


def changed_files(before: dict, after: dict) -> list[str]:
    return sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))


def bwrap_argv(job: dict, run_dir: Path, workroot: Path, git_info: dict) -> list[str]:
    home = Path(job["home"])
    argv = ["bwrap", "--die-with-parent", "--unshare-pid", "--new-session",
            "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    visible = None
    if job.get("home_mode") == "allowlist":
        # Whitelist accounts: an empty home with only their read roots and
        # OpenCode's own runtime mounted back, read-only.
        argv += ["--tmpfs", str(home)]
        visible = []
        for raw in list(job.get("read_roots", [])) + list(job.get("runtime_paths", [])):
            if Path(raw).exists():
                argv += ["--ro-bind", raw, raw]
                visible.append(raw.rstrip("/"))

    def shown(raw: str) -> bool:
        return visible is None or any(raw == v or raw.startswith(v + "/") for v in visible)

    for raw in job.get("mask_dirs", []):
        if Path(raw).is_dir() and shown(raw):
            argv += ["--tmpfs", raw]
    for raw in job.get("mask_files", []):
        if Path(raw).is_file() and shown(raw):
            argv += ["--ro-bind", "/dev/null", raw]
    state = run_dir / "state"
    for sub in ("data", "state", "tmp"):
        (state / sub).mkdir(parents=True, exist_ok=True)
    cache = home / ".cache" / "opencode"
    cache.mkdir(parents=True, exist_ok=True)
    argv += ["--bind", str(state), str(state), "--bind", str(cache), str(cache)]
    if git_info.get("git") and git_info.get("writable"):
        top = Path(git_info["top"])
        argv += ["--bind", str(top), str(top)]
    else:
        argv += ["--bind", str(workroot), str(workroot)]
    # Files inside the workdir the caller may not read (a denied .env, say).
    for found in _denied_files_under(workroot, job.get("deny_patterns", [])):
        argv += ["--ro-bind", "/dev/null", str(found)]
    # Masks again on top of the writable bind, for denied folders inside it.
    for raw in job.get("mask_dirs", []):
        if Path(raw).is_dir() and raw.startswith(str(workroot).rstrip("/") + "/"):
            argv += ["--tmpfs", raw]
    argv += ["--chdir", str(workroot)]
    return argv


def _denied_files_under(root: Path, patterns, limit: int = 20000):
    patterns = [str(p) for p in patterns or () if p]
    if not patterns:
        return
    checked = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in {".git", "node_modules", ".venv", "venv", "__pycache__"}]
        for name in filenames:
            checked += 1
            if checked > limit:
                return
            full = os.path.join(dirpath, name)
            if any(fnmatch.fnmatch(full, pat) or fnmatch.fnmatch(name, pat.rsplit("/", 1)[-1])
                   and pat.startswith("**/") for pat in patterns):
                yield Path(full)


def sandbox_env(job: dict, run_dir: Path, key: str) -> dict:
    home = job["home"]
    state = run_dir / "state"
    env = {
        "HOME": home,
        "PATH": job.get("path") or "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "XDG_DATA_HOME": str(state / "data"),
        "XDG_STATE_HOME": str(state / "state"),
        "XDG_CACHE_HOME": str(Path(home) / ".cache"),
        "XDG_CONFIG_HOME": str(Path(home) / ".config"),
        "TMPDIR": "/tmp",
        "PYTHONDONTWRITEBYTECODE": "1",
        "OPENCODE_RECURSION_GUARD": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_CONFIG_CONTENT": json.dumps(job["opencode_config"]),
        "OMNIROUTE_API_KEY": key,
    }
    return env


def take_key(run_dir: Path) -> str:
    path = run_dir / "omniroute.key"
    try:
        key = path.read_text().strip()
    except OSError:
        key = ""
    try:
        path.unlink()
    except OSError:
        pass
    if not key:
        fallback = Path(os.path.expanduser("~/.config/opencode/omniroute.key"))
        try:
            key = fallback.read_text().strip()
        except OSError:
            key = ""
    return key


def collect_git(git_info: dict, workroot: Path, files: list[str]) -> str:
    """Diff of exactly the files this job changed, against HEAD (untracked as new)."""
    if not files:
        return ""
    top = Path(git_info["top"])
    _, patch = _run(["git", "-C", str(top), "diff", "HEAD", "--", *files], timeout=120)
    _, untracked = _run(["git", "-C", str(top), "ls-files", "--others", "--exclude-standard", "--", *files], timeout=60)
    for rel in [u for u in untracked.splitlines() if u.strip()]:
        _, extra = _run(["git", "-C", str(top), "diff", "--no-index", "--", "/dev/null", rel], timeout=60)
        patch += extra
    lines = patch.splitlines()
    shown = "\n".join(lines[:REPORT_DIFF_LINES])
    if len(lines) > REPORT_DIFF_LINES:
        shown += f"\n[... {len(lines) - REPORT_DIFF_LINES} more diff lines; run git diff in the workdir]"
    return shown


def main(job_dir: str) -> int:
    run_dir = Path(job_dir)
    job = json.loads((run_dir / "job.json").read_text())
    started = time.time()
    report = [f"# opencode_task {job['id']}", f"task: {' '.join(job['task'].split())[:300]}"]

    key = take_key(run_dir)
    if os.environ.get("OPENCODE_RECURSION_GUARD"):
        print("\n".join(report + ["status: refused", "reason: already inside an OpenCode run (recursion guard)"]))
        return 2
    if not shutil.which("bwrap"):
        print("\n".join(report + ["status: refused", "reason: bubblewrap (bwrap) is not installed; refusing to run OpenCode unconfined"]))
        return 2
    slot = acquire_slot(Path(job["slot_dir"]), int(job.get("slots", 12)), int(job.get("slot_wait", 600)))
    if slot is None:
        print("\n".join(report + ["status: busy",
                                  f"reason: all {job.get('slots', 12)} OpenCode slots stayed busy; retry later"]))
        return 75
    try:
        workroot = Path(job["workdir"])
        git_info = {"git": bool(job.get("git_top")), "top": job.get("git_top", ""),
                    "writable": bool(job.get("git_writable"))}
        before = snapshot(workroot, git_info["top"])
        argv = bwrap_argv(job, run_dir, workroot, git_info) + [
            job["opencode_bin"], "run", "-m", job["model"], "--dir", str(workroot), job["task"]]
        env = sandbox_env(job, run_dir, key)
        key = ""
        code, output = _run(argv, cwd=str(workroot), env=env, timeout=int(job.get("timeout", 3600)))
        report.append(f"opencode exit: {code}")
        test_out = ""
        if job.get("test_command"):
            test_argv = bwrap_argv(job, run_dir, workroot, git_info) + ["bash", "-c", job["test_command"]]
            env_test = dict(env)
            env_test.pop("OMNIROUTE_API_KEY", None)
            tcode, test_out = _run(test_argv, cwd=str(workroot), env=env_test, timeout=int(job.get("test_timeout", 900)))
            report.append(f"test exit: {tcode}")
        after = snapshot(workroot, git_info["top"])
        changed = changed_files(before, after)
        report += ["", "## Changed files (edited in place in " + str(workroot) + ")"] + (changed or ["(none)"])
        if git_info.get("git"):
            patch = collect_git(git_info, workroot, changed)
            if patch:
                report += ["", "## Diff (against HEAD, only files this job changed)", patch]
            report += ["", "Nothing is committed: review, then commit or `git checkout -- <file>` to undo."]
        if job.get("test_command"):
            report += ["", "## Test output", _tail(test_out)]
        report += ["", "## OpenCode output", _tail(output)]
        report.append(f"\nstatus: {'ok' if code == 0 else 'failed'} in {int(time.time() - started)}s")
        print("\n".join(report))
        return 0 if code == 0 else 1
    finally:
        slot.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: opencode_task_runner.py <job-dir>", file=sys.stderr)
        sys.exit(64)
    sys.exit(main(sys.argv[1]))
