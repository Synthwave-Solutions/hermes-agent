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
    # The original scope survives continuation/subprocess transport, while a
    # callback must be rebound by the trusted host before it can authorize.
    assert restored.project_workspace == str(root) and restored.project_access_check is None
    assert not decide(restored,"read_file",{"path":str(path)}).allowed


def test_revocation_wins_even_with_global_root(tmp_path):
    subject=GovernanceSubject(email="member@example.test")
    ctx=DashboardGovernanceContext(subject,EffectiveAccess(subject=subject,mode="enforce",grants=GrantSet(file_read_roots=frozenset({"*"}))),project_workspace=str(tmp_path),project_access_check=lambda p,w:False)
    assert not decide(ctx,"read_file",{"path":str(tmp_path/"file")}).allowed


def test_real_relative_file_dispatch_and_last_moment_revocation(tmp_path, monkeypatch):
    import json
    import model_tools
    from hermes_cli.dashboard_governance.context import governance_context
    from tools import file_tools
    from types import SimpleNamespace
    root=tmp_path/"project";root.mkdir();(root/"plan.txt").write_text("actual project plan")
    subject=GovernanceSubject(email="member@example.test")
    access=EffectiveAccess(subject=subject,mode="enforce",grants=GrantSet(tools=frozenset({"read_file"}),file_read_roots=frozenset({str(tmp_path/"own")})))
    calls=[]
    def check(path,write):calls.append(path);return True
    ctx=DashboardGovernanceContext(subject,access,project_workspace=str(root),project_access_check=check)
    monkeypatch.setattr(file_tools,"_resolve_base_dir",lambda task_id:root)
    monkeypatch.setattr(file_tools,"_uses_container_paths",lambda task_id:False)
    monkeypatch.setattr(model_tools.registry,"get_toolset_for_tool",lambda name:"file")
    monkeypatch.setattr(model_tools.registry,"get_entry",lambda name:SimpleNamespace(toolset="file",schema={}))
    # Exercise the real file handler, not a fabricated policy result.
    monkeypatch.setattr(model_tools.registry,"dispatch",lambda name,args,**kw:file_tools._handle_read_file(args,**kw))
    with governance_context(ctx):
        result=model_tools.handle_function_call("read_file",{"path":"plan.txt"},task_id="project-test",skip_pre_tool_call_hook=True,skip_tool_request_middleware=True,skip_tool_execution_middleware=True)
    assert "actual project plan" in result
    assert len(calls)>=3 and all(path==str(root/"plan.txt") for path in calls)
    count=[0]
    def revoke(path,write):count[0]+=1;return count[0]==1
    with governance_context(replace(ctx,project_access_check=revoke)):
        result=model_tools.handle_function_call("read_file",{"path":"plan.txt"},task_id="project-test",skip_pre_tool_call_hook=True,skip_tool_request_middleware=True,skip_tool_execution_middleware=True)
    assert "denied before execution" in result and "actual project plan" not in result

    monkeypatch.setattr("hermes_cli.plugins._dispatch_pre_tool_call_hooks",lambda *a,**kw:(None,{"path":str(tmp_path/"outside.txt")}))
    with governance_context(ctx):
        result=model_tools.handle_function_call("read_file",{"path":"plan.txt"},task_id="project-test",skip_tool_request_middleware=True,skip_tool_execution_middleware=True)
    assert "denied before execution" in result
