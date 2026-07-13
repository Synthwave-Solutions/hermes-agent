from __future__ import annotations

import argparse
import json

from hermes_cli.dashboard_governance import cli as governance_cli


def test_governance_cli_init_validate_preview(tmp_path, capsys):
    path = tmp_path / "policy.yaml"

    rc = governance_cli.cmd_init(argparse.Namespace(policy=str(path), force=False))
    assert rc == 0
    assert path.exists()

    rc = governance_cli.cmd_validate(argparse.Namespace(policy=str(path)))
    assert rc == 0
    out = capsys.readouterr().out
    assert "valid:" in out
    assert "mode: report_only" in out

    rc = governance_cli.cmd_preview(
        argparse.Namespace(
            policy=str(path),
            email="admin@example.com",
            user_id="",
            provider="cli",
            role=None,
            group=None,
        )
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "report_only"
    assert payload["is_admin"] is True
    assert "*" in payload["permissions"]


def test_governance_cli_init_refuses_overwrite_without_force(tmp_path):
    path = tmp_path / "policy.yaml"
    path.write_text("version: 1\nmode: off\n", encoding="utf-8")

    rc = governance_cli.cmd_init(argparse.Namespace(policy=str(path), force=False))

    assert rc == 1
    assert path.read_text(encoding="utf-8") == "version: 1\nmode: off\n"
