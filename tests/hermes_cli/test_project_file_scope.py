from dataclasses import replace
from hermes_cli.dashboard_governance.context import DashboardGovernanceContext, serialize_context_for_env, context_from_env_payload
from hermes_cli.dashboard_governance.models import EffectiveAccess, GovernanceSubject, GrantSet
from hermes_cli.dashboard_governance.tool_policy import tool_arguments_allowed_for_context as decide


def test_fresh_membership_scoped_grant_and_revocation(tmp_path):
    root=tmp_path/"project"; root.mkdir(); path=root/"notes.md"; path.write_text("notes")
    subject=GovernanceSubject(email="member@example.test")
    access=EffectiveAccess(subject=subject,mode="enforce",grants=GrantSet(file_read_roots=frozenset({str(tmp_path/"own")}),file_write_roots=frozenset({str(tmp_path/"own")})))
    member=[True]; calls=[]
    def check(path,write):calls.append((path,write));return member[0] and not write
    ctx=DashboardGovernanceContext(subject,access,project_workspace=str(root),project_access_check=check)
    assert decide(ctx,"read_file",{"path":str(path)}).allowed
    assert not decide(ctx,"write_file",{"path":str(path)}).allowed
    member[0]=False
    assert not decide(ctx,"read_file",{"path":str(path)}).allowed
    assert len(calls)==3
    member[0]=True
    assert not decide(ctx,"read_file",{"path":str(tmp_path/"private")}).allowed
    link=root/"linked";link.symlink_to(path)
    assert not decide(ctx,"read_file",{"path":str(link)}).allowed
    assert not decide(ctx,"read_file",{"path":str(root/"a"/".."/"notes.md")}).allowed
    denied=replace(ctx,access=replace(access,grants=replace(access.grants,file_denied_globs=frozenset({"*.md"}))))
    assert not decide(denied,"read_file",{"path":str(path)}).allowed
    restored=context_from_env_payload(serialize_context_for_env(ctx))
    assert not restored.project_workspace and restored.project_access_check is None
    assert not decide(restored,"read_file",{"path":str(path)}).allowed


def test_revocation_wins_even_with_global_root(tmp_path):
    subject=GovernanceSubject(email="member@example.test")
    ctx=DashboardGovernanceContext(subject,EffectiveAccess(subject=subject,mode="enforce",grants=GrantSet(file_read_roots=frozenset({"*"}))),project_workspace=str(tmp_path),project_access_check=lambda p,w:False)
    assert not decide(ctx,"read_file",{"path":str(tmp_path/"file")}).allowed
