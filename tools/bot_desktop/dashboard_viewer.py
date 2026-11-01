"""Whether the engine dashboard may act as a bot screen viewer (``bot_desktop.dashboard_viewer``).

The upstream display bridge and viewer RPCs must not create a second screen-access path around
SynthPulse's viewer permissions, privacy and audit policy. They stay off unless the launch
profile explicitly sets this flag to true. The route is mounted at startup; connections and
RPCs re-check the flag. This gate does not itself implement the SynthPulse viewer.

The dashboard's own launch profile decides, never a profile a request is scoped to, so one profile's
config cannot open the viewer for the whole dashboard.
"""

from __future__ import annotations


def enabled() -> bool:
    """``bot_desktop.dashboard_viewer`` of the process's launch profile; anything but ``True`` is off,
    and so is a config that cannot be read."""
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    token = set_hermes_home_override(None)  # the launch profile, not a request's profile scope
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly().get("bot_desktop") or {}
    except Exception:
        return False
    finally:
        reset_hermes_home_override(token)
    return isinstance(cfg, dict) and cfg.get("dashboard_viewer") is True
