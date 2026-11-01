import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "synthwave-policy"
EXAMPLE_POLICY = REPO / "policy" / "autonomy.example.yaml"
sys.path.insert(0, str(PLUGIN_DIR))

import policy_engine as pe  # noqa: E402


def load_plugin_package():
    """Import the plugin directory as a package (relative imports work, as in Hermes)."""
    name = "synthwave_policy_under_test"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, str(PLUGIN_DIR / "__init__.py"), submodule_search_locations=[str(PLUGIN_DIR)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def example_policy():
    return pe.load_policy_file(EXAMPLE_POLICY)


@pytest.fixture()
def plugin():
    return load_plugin_package()


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME and root so tests never touch ~/.hermes."""
    h = tmp_path / "home"
    (h / "policy").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("SYNTHWAVE_POLICY_ROOT", str(tmp_path / "root"))
    return h
