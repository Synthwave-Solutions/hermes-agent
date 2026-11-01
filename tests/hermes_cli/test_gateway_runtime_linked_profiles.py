"""Profiles linked into a separate multiplexer keep their canonical identity."""
import asyncio
from pathlib import Path

import pytest

from tests.hermes_cli.test_gateway_runtime import control_peer


@pytest.mark.platforms("linux", "macos")
@pytest.mark.parametrize(
    "case", ["served", "unserved", "stale", "foreign", "wrong_link",
             "cyclic_link", "cyclic_link_legacy"]
)
def test_linked_profile_discovers_only_its_live_serving_owner(tmp_path, monkeypatch, case):
    import hermes_cli.dashboard_procs as procs
    import hermes_cli.gateway_runtime as runtime

    root = tmp_path / "primary"
    home = root / "profiles" / "scout"
    home.mkdir(parents=True, mode=0o700)
    host = tmp_path / "multiplexer"
    identity = str(home.resolve())
    owners = {home: None if case == "stale" else 12345,
              host: 54321 if case == "foreign" else 12345}
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(root))
    # Process introspection is the OS boundary; the real private control socket and
    # descriptor validation still have to prove that the candidate serves this home.
    monkeypatch.setattr(runtime, "_owner_pid", lambda path: owners.get(path))
    monkeypatch.setattr(procs, "_hermes_home_for_pid", lambda pid: str(host))
    payload = {
        "runtime_protocol": 1, "state": "ready", "instance_id": "mux-owner",
        "authority_epoch": 1, "supervisor": "none", "api_origin": "http://127.0.0.1:8765",
        "capabilities": ["session-authority-v1"],
        "served_profiles": [] if case == "unserved" else [{"home": identity, "profile_id": identity}],
    }

    async def probe():
        async with control_peer(host, payload):
            links = host / "profiles"
            links.mkdir()
            link = links / home.name
            target = {"wrong_link": root, "cyclic_link": link,
                      "cyclic_link_legacy": link}.get(case, home)
            link.symlink_to(target)
            if case == "cyclic_link_legacy":
                original_resolve = Path.resolve

                def legacy_resolve(path, strict=False):
                    if path == link:
                        # Python <=3.12 raises RuntimeError for symlink loops.
                        raise RuntimeError(f"Symlink loop from {path}")
                    return original_resolve(path, strict=strict)

                monkeypatch.setattr(Path, "resolve", legacy_resolve)
            verdict = await asyncio.to_thread(runtime.discover_gateway_endpoint, home, timeout=1)
            if case == "served":
                assert verdict.state == "ready", verdict
                assert verdict.endpoint.profile_id == identity
                assert verdict.endpoint.control_home == str(host.resolve())
                assert verdict.endpoint.instance_id == "mux-owner"
            else:
                assert verdict.endpoint is None, verdict
                assert verdict.state != "ready"

    asyncio.run(probe())
