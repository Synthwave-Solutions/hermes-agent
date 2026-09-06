# Project file scope

DashboardGovernanceContext accepts project_workspace and project_access_check(path, write).
The trusted WebUI binds them per invocation. They are in-memory capabilities and are never
serialized to child process environments. The callback must freshly verify project membership,
actor file permissions, project bot membership and path containment. Exceptions fail closed.

Only absolute paths inside that workspace can receive a read_file/search_files/write_file/patch
root exception. Symlink components and traversal are denied. Explicit denied globs and the
normal profile/tool gates remain authoritative. Every project file invocation rechecks membership
even when the principal has a broader global file root. Terminal, MCP and external workers receive
no extra grant. At model_tools dispatch, relative local file paths are resolved once using the file backend task cwd, then the same absolute path is checked and executed. The final dispatch checks again after plugins and execution middleware. Container relative paths receive no extra scope. Normal filesystem races are not eliminated by this policy check; file tool backend
isolation remains responsible for atomic IO confinement.

Tests: scripts/run_tests.sh tests/hermes_cli/test_project_file_scope.py tests/test_governance_tool_runtime.py
