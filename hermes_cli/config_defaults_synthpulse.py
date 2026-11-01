"""SynthPulse installation defaults. Pure data; safe for config bootstrap.

Nullable migration settings let existing installations keep their legacy launch
variables. An explicit profile config takes precedence, including false and 0.
"""

SYNTHPULSE_DEFAULTS = {
    # Bot Desktop: a headless Xfce screen per profile on the gateway host (Linux), streamed to Hermes
    # Desktop where a human can watch, take over (logins, 2FA, CAPTCHAs) and hand back. `hermes computer-use screen`.
    "bot_desktop": {
        # SynthPulse owns viewer permissions, privacy and audit. Engine viewer requires explicit opt-in.
        "dashboard_viewer": False,
        "geometry": "1440x900",
        # Opt-in: start the screen automatically the first time computer_use needs a display on a headless
        # host. Off by default so installing TigerVNC for other reasons never yields a screen nobody asked
        # for; Hermes Desktop's Screen pane offers Start and this toggle.
        "auto_start": False,
        # Refuse to start below this much free memory (MB), measured on the host or its container cgroup,
        # whichever is tighter. Xvnc + Xfce idle at ~220 MB and a takeover's browser adds 0.5-1 GB, so a
        # screen with one page runs past 1 GB; the kernel OOM killer picks its victim by score, so on a
        # small instance the loser is the dashboard or the gateway rather than the desktop. 0 disables the
        # check.
        "min_free_memory_mb": 1536,
        # Stop a screen nobody has used (no computer_use action, browser spawn, viewer or takeover) for this
        # long; it restarts on the next use. Idle Xvnc + Xfce hold ~220 MB, an abandoned browser far more.
        # 0 keeps screens up until stopped.
        "idle_stop_minutes": 30,
        # Where the screen (and with it computer_use and the bot's browser) runs.
        #   auto      follow the terminal backend: inside the docker / ssh / streaming plugin sandbox when one is
        #             configured, on the gateway host when terminal.backend is local. A sandbox backend that
        #             cannot host a screen (modal, daytona, vercel) REFUSES rather than quietly running the
        #             desktop on the host beside the sandbox you chose for the agent.
        #   terminal  always inside the terminal backend (error when it cannot host one).
        #   gateway   always on the gateway host, even with a sandbox terminal: the agent's screen, browser
        #             and computer_use then act OUTSIDE the terminal sandbox. Explicit opt-in.
        # The sandbox image needs the desktop stack: nousresearch/hermes-sandbox:desktop.
        "placement": "auto",
    },
    # SynthPulse model gateway contract. On a managed installation every
    # model call goes through the installation's own gateway, and the
    # installation renders this block (base_url, enforce, organisation,
    # installation and module). These defaults are inert: with enforce
    # false and no gateway URL every provider path works as before. Fields:
    #   base_url         the gateway's OpenAI-compatible base URL ("" = none)
    #   enforce          refuse any model route that is not the gateway
    #   org_id           organisation id stamped on traces
    #   installation_id  installation id stamped on traces
    #   module           the calling module's name in the key registry
    #   user_field       how the OpenAI "user" field is filled ("pseudonym")
    #   session_header   request header that carries the session id
    #   realtime         true only when the gateway carries a realtime route
    #   tool_exceptions  tool backends still allowed under enforce; rendered
    #                    only at the operator's own installation from its
    #                    exceptions file, never on a managed installation
    "model_gateway": {
        "base_url": "",
        "enforce": False,
        "org_id": "",
        "installation_id": "",
        "module": "engine",
        "user_field": "pseudonym",
        "session_header": "X-SP-Session",
        "realtime": False,
        "tool_exceptions": [],
    },
    "opencode": {
        "binary": None,  # Legacy HERMES_OPENCODE_BIN; otherwise the installed CLI.
        "max_slots": None,  # Legacy HERMES_OPENCODE_MAX_SLOTS; otherwise 12.
    },
    "webui": {
        "state_dir": None,  # Legacy host spool; otherwise <profile>/webui.
        "attachment_dir": None,  # Legacy host inbox; otherwise <profile>/webui/attachments.
    },
    "api_server": {
        "disable_caller_governance": None,  # Legacy opt-out; otherwise false (caller governed).
    },
}
