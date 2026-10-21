"""Table-driven checks of the shipped example policy.

False-positive traps and the decisions taken for them:
* A sending command that only appears as TEXT (inside echo/printf quotes, a grep pattern, a comment,
  a heredoc fed to cat or tee, a commit message) is AUTO: nothing is executed.
* A heredoc fed to a shell (bash <<EOF), bash -c '...', eval, ssh host '...' and $( ) ARE executed
  and are therefore checked.
* Writing a script file that contains "waapi send" is AUTO; running it is not detected (known gap,
  the policy is a guard rail, not a sandbox).
"""

import pytest

from conftest import pe

T = "terminal"


def term(cmd):
    return (T, {"command": cmd})


CASES = [
    # ---- read-only and harmless: AUTO -------------------------------------------------------
    ("waapi status", *term("waapi status"), "AUTO"),
    ("waapi chats", *term("waapi chats"), "AUTO"),
    ("waapi messages", *term("waapi messages 31612345678@c.us --limit 20"), "AUTO"),
    ("waapi send-seen action", *term("waapi action send-seen --data '{\"chatId\": \"x\"}'"), "AUTO"),
    ("gws gmail list", *term("gws gmail users messages list --params '{\"q\": \"send\"}'"), "AUTO"),
    ("gws gmail triage", *term("gws gmail +triage"), "AUTO"),
    ("gws gmail draft create", *term("gws gmail users drafts create --json '{}'"), "AUTO"),
    ("gchat messages", *term("gchat messages spaces/AAA --limit 5"), "AUTO"),
    ("gchat dm lookup", *term("gchat dm someone@synthwave.solutions"), "AUTO"),
    ("team-queue list", *term("team-queue list"), "AUTO"),
    ("team-queue preview", *term("team-queue --json preview 12"), "AUTO"),
    ("team-queue lint", *term("team-queue lint 12"), "AUTO"),
    ("mailchimp draft", *term("mailchimp draft --html m.html --subject S --preheader P"), "AUTO"),
    ("mailchimp test", *term("mailchimp test abc --to x@synthwave.solutions"), "AUTO"),
    ("linkup inbox", *term("linkup inbox --total 10"), "AUTO"),
    ("linkup comments read", *term("linkup comments --post urn:li:activity:1"), "AUTO"),
    ("git push feature", *term("git push origin feat/new-thing"), "AUTO"),
    ("git push main-like branch", *term("git push -u origin main-fix"), "AUTO"),
    ("git status", *term("git status && git log --oneline -5"), "AUTO"),
    ("cat env", *term("cat ~/.hermes/.env | grep -c KEY"), "AUTO"),
    ("sqlite select", *term("sqlite3 -readonly ~/omniroute/data/storage.sqlite 'SELECT count(*) FROM usage'"), "AUTO"),
    ("rm -rf tmp", *term("rm -rf /tmp/build-123"), "AUTO"),
    ("rm -rf node_modules", *term("rm -rf node_modules"), "AUTO"),
    ("rm single file", *term("rm notes.txt"), "AUTO"),
    ("docker ps", *term("docker ps -a && docker logs omniroute --tail 50"), "AUTO"),
    ("systemctl status", *term("systemctl --user status hermes-webui"), "AUTO"),
    ("bunq read-only", *term("bunq --json balance"), "AUTO"),
    ("pytest send test", *term("python3 -m pytest tests/test_send.py"), "AUTO"),
    # ---- false-positive traps: AUTO -----------------------------------------------------------
    ("echo quoted send", *term('echo "waapi send --to 31 --text hi"'), "AUTO"),
    ("printf quoted send", *term("printf 'run: gchat send spaces/x hi\\n'"), "AUTO"),
    ("grep for send", *term("grep -rn 'waapi send' ~/.hermes/skills"), "AUTO"),
    ("comment send", *term("ls  # later: waapi send --to x"), "AUTO"),
    ("heredoc to cat", *term("cat > /tmp/plan.md <<'EOF'\nwaapi send --to 31 --text hi\ngchat send spaces/x hi\nEOF"), "AUTO"),
    ("commit message", *term('git commit -m "add waapi send wrapper and git push --force docs"'), "AUTO"),
    ("man page", *term("waapi send --help"), "ASK"),  # documented: --help still matches (conservative)
    ("write file with send text", "write_file", {"path": "/tmp/x.sh", "content": "waapi send --to 1"}, "AUTO"),
    ("env example", "write_file", {"path": "/repo/.env.example", "content": "KEY="}, "AUTO"),
    ("read env file tool", "read_file", {"path": "/repo/.env"}, "AUTO"),
    ("gmail search mcp", "mcp__claude_ai_Gmail__search_threads", {"query": "send"}, "AUTO"),
    ("gmail create draft", "mcp__claude_ai_Gmail__create_draft", {"to": "a@b.nl"}, "AUTO"),
    ("calendar list", "mcp__claude_ai_Google_Calendar__list_events", {}, "AUTO"),
    ("lovable get project", "mcp__claude_ai_Lovable__get_project", {"project_id": "x"}, "AUTO"),
    ("stripe list payouts", "mcp__stripe__list_payouts", {}, "AUTO"),
    ("mailchimp draft mcp", "mcp__mailchimp__create_draft_campaign", {}, "AUTO"),
    ("send_message list", "send_message", {"action": "list"}, "AUTO"),
    ("send_message telegram home", "send_message", {"target": "telegram", "message": "done"}, "AUTO"),
    # ---- outbound: ASK -------------------------------------------------------------------------
    ("waapi send", *term("waapi send --to 31612345678 --text 'Hoi'"), "ASK"),
    ("waapi instance send-media", *term("waapi --instance 42 send-media --to 31 --file a.pdf"), "ASK"),
    ("waapi action send-message", *term("waapi action send-message --data '{}'"), "ASK"),
    ("gws gmail +send", *term("gws gmail +send --to a@b.nl --subject Hi --body x"), "ASK"),
    ("gws-hermes messages send", *term("gws-hermes gmail users messages send --json @raw.json"), "ASK"),
    ("sendmail", *term("sendmail -t < /tmp/mail.eml"), "ASK"),
    ("gchat send with as", *term("gchat --as hrishi@synthwave.solutions send spaces/AAA 'hi'"), "ASK"),
    ("gchat via sudo timeout", *term("sudo -u synthwavehq timeout 30 gchat reply spaces/A/threads/B hi"), "ASK"),
    ("team-queue add", *term("team-queue add --audience client --to a@b.nl --body-file b.txt"), "ASK"),
    ("team-queue run", *term("team-queue --dir /x run"), "ASK"),
    ("linkup send", *term("linkup send --url https://linkedin.com/in/x --text hi"), "ASK"),
    ("mailchimp schedule cli", *term("mailchimp schedule abc --at 2026-10-21T08:00 --approved 'ok'"), "ASK"),
    ("bash -c send", *term("bash -c 'waapi send --to 31 --text hi'"), "ASK"),
    ("heredoc to bash", *term("bash <<'EOF'\nwaapi send --to 31 --text hi\nEOF"), "ASK"),
    ("ssh remote send", *term("ssh pi 'gchat send spaces/x hi'"), "ASK"),
    ("subst send", *term('echo "$(waapi send --to 31 --text hi)"'), "ASK"),
    ("chained send", *term("cd /tmp && waapi status && waapi send --to 31 --text hi"), "ASK"),
    ("env prefix send", *term("WAAPI_INSTANCE_ID=1 /home/x/.local/bin/waapi send --to 31 --text hi"), "ASK"),
    ("gmail mcp send", "mcp__claude_ai_Gmail__send_message", {"to": "a@b.nl"}, "ASK"),
    ("gmail mcp reply", "mcp__claude_ai_Gmail__reply", {"thread_id": "t"}, "ASK"),
    ("gmail mcp forward legacy name", "mcp_gmail_forward", {}, "ASK"),
    ("mailchimp schedule mcp", "mcp__mailchimp__schedule_campaign", {"campaign_id": "x"}, "ASK"),
    ("lemlist start", "mcp__lemlist__start_campaign", {"campaignId": "c"}, "ASK"),
    ("send_message whatsapp", "send_message", {"target": "whatsapp:31612345678", "message": "hi"}, "ASK"),
    ("composio gmail", "mcp__composio__COMPOSIO_MULTI_EXECUTE_TOOL", {"tools": [{"tool_slug": "GMAIL_SEND_EMAIL"}]}, "ASK"),
    ("marktplaats offer", "mcp__pareltje__send_offer", {}, "ASK"),
    # ---- calendar: ASK -------------------------------------------------------------------------
    ("gcal create", "mcp__claude_ai_Google_Calendar__create_event", {"summary": "x"}, "ASK"),
    ("gcal delete", "mcp__claude_ai_Google_Calendar__delete_event", {"event_id": "x"}, "ASK"),
    ("notion calendar", "mcp__claude_ai_Notion__notion_calendar_create_events", {}, "ASK"),
    ("gws calendar insert", *term("gws calendar +insert --summary x --start 2026-10-20T10:00"), "ASK"),
    # ---- credentials: BLOCK / ASK -------------------------------------------------------------
    ("write .env", "write_file", {"path": "/repo/.env", "content": "KEY=x"}, "BLOCK"),
    ("patch portal.env", "patch", {"path": "/c/creds/portal.env", "old_string": "a", "new_string": "b"}, "BLOCK"),
    ("write ssh key", "write_file", {"path": "/home/x/.ssh/authorized_keys", "content": "ssh-ed25519"}, "BLOCK"),
    ("shell echo to env", *term("echo KEY=1 >> /repo/.env.local"), "ASK"),
    ("passwd", *term("sudo passwd michael"), "BLOCK"),
    ("gcloud key create", *term("gcloud --project p iam service-accounts keys create k.json --iam-account a@b"), "BLOCK"),
    ("supabase update password", "execute_code",
     {"code": "sb.auth.admin.update_user_by_id(uid, {'password': 'x'})"}, "BLOCK"),
    ("psql alter user", *term("psql -c \"ALTER USER app WITH PASSWORD 'x'\""), "BLOCK"),
    # ---- OmniRoute storage: BLOCK -------------------------------------------------------------
    ("sqlite update storage", *term("sqlite3 ~/omniroute/data/storage.sqlite \"UPDATE combos SET x=1\""), "BLOCK"),
    ("sqlite heredoc storage", *term("sqlite3 storage.sqlite <<'SQL'\nDELETE FROM usage;\nSQL"), "BLOCK"),
    ("cp over storage", *term("cp /tmp/backup.sqlite ~/omniroute/data/storage.sqlite"), "BLOCK"),
    ("docker exec storage ok", *term("docker exec omniroute sqlite3 /app/data/storage.sqlite \"UPDATE x SET y=1\""), "AUTO"),
    # ---- git -----------------------------------------------------------------------------------
    ("force push", *term("git push --force origin feat/x"), "ASK"),
    ("force push short", *term("git push -f origin feat/x"), "ASK"),
    ("force with lease", *term("git -C /repo push --force-with-lease"), "ASK"),
    ("plus refspec", *term("git push origin +feat/x"), "ASK"),
    ("push main", *term("git push origin main"), "ASK"),
    ("push HEAD:master", *term("git push origin HEAD:master"), "ASK"),
    # ---- destructive ---------------------------------------------------------------------------
    ("rm -rf home dir", *term("rm -rf ~/clients/alta-ict/old"), "ASK"),
    ("rm -rf traversal", *term("rm -rf /tmp/../home/synthwavehq/x"), "ASK"),
    ("rm -rf mixed", *term("rm -rf /tmp/a /home/synthwavehq/b"), "ASK"),
    ("rm -rf variable", *term('rm -rf "$TARGET"'), "ASK"),
    ("docker rm", *term("docker rm -f omniroute"), "ASK"),
    ("docker compose down", *term("docker compose -f x.yml down"), "ASK"),
    ("systemctl restart hermes", *term("systemctl --user restart hermes-webui"), "ASK"),
    ("pkill omniroute", *term("pkill -f omniroute"), "ASK"),
    # ---- deploy ---------------------------------------------------------------------------------
    ("lovable deploy", "mcp__claude_ai_Lovable__deploy_project", {"project_id": "abc"}, "ASK"),
    ("lovable deploy helix", "mcp__claude_ai_Lovable__deploy_project",
     {"project_id": "90a96aee-8bc6-49c9-8c8c-479df5a161db"}, "BLOCK"),
    # ---- money ----------------------------------------------------------------------------------
    ("stripe charge mcp", "mcp__stripe__create_charge", {"amount": 100}, "BLOCK"),
    ("stripe payout cli", *term("stripe payouts create --amount 100"), "BLOCK"),
    ("curl bunq payment", *term("curl -X POST https://api.bunq.com/v1/user/1/monetary-account/2/payment -d '{}'"), "BLOCK"),
    ("marktplaats purchase", "mcp__pareltje__authorize_purchase_wish", {}, "BLOCK"),
]


@pytest.mark.parametrize("label,tool,args,expected", CASES, ids=[c[0] for c in CASES])
def test_example_policy_decision(example_policy, label, tool, args, expected):
    d = pe.evaluate(example_policy, tool, args, "default")
    assert d.decision == expected, (label, d.to_dict())


def test_case_count():
    assert len(CASES) >= 40


def test_example_policy_is_observe(example_policy):
    assert example_policy.mode == "observe"


def test_rule_ids_unique_and_described(example_policy):
    ids = [r.id for r in example_policy.rules]
    assert len(ids) == len(set(ids))
    assert all(r.description and r.description != r.id for r in example_policy.rules)


def test_no_long_dashes_in_shipped_text():
    from conftest import EXAMPLE_POLICY, PLUGIN_DIR, REPO
    files = [EXAMPLE_POLICY, *PLUGIN_DIR.glob("*.py"), *PLUGIN_DIR.glob("*.yaml"), *(REPO / "bin").glob("*.py"),
             REPO / "install.sh"]
    for f in files:
        text = f.read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, f
