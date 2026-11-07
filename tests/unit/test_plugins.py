"""Plugin loader: local .daemon/plugins modules and entry points."""

from daemon.plugins import load_local_plugins, load_plugins
from daemon.tools import ToolRegistry


def _plugin_module(workspace, name, body):
    plugin_dir = workspace / ".daemon" / "plugins"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / f"{name}.py").write_text(body)


def test_local_plugin_registers_tool(workspace):
    _plugin_module(workspace, "my_tools", """
from daemon.tools.base import Tool, ToolOutput


class HelloTool(Tool):
    name = "hello_plugin"
    description = "Says hello."
    read_only = True

    async def run(self, args, ctx):
        return ToolOutput(content="hello from plugin")


def register(registry):
    registry.register(HelloTool())
""")
    registry = ToolRegistry()
    result = load_local_plugins(workspace, ["my_tools"], registry)
    assert result.loaded == ["my_tools"]
    assert result.failed == []
    assert "hello_plugin" in registry.names()


def test_missing_plugin_is_reported_not_fatal(workspace):
    registry = ToolRegistry()
    result = load_local_plugins(workspace, ["nope"], registry)
    assert result.loaded == []
    assert result.failed and result.failed[0][0] == "nope"


def test_plugin_without_register_hook_is_reported(workspace):
    _plugin_module(workspace, "empty", "X = 1\n")
    registry = ToolRegistry()
    result = load_local_plugins(workspace, ["empty"], registry)
    assert result.loaded == []
    assert "register" in result.failed[0][1]


def test_broken_plugin_does_not_crash_loading(workspace):
    _plugin_module(workspace, "boom", "raise RuntimeError('kaboom')\n")
    _plugin_module(workspace, "good", """
def register(registry):
    pass
""")
    registry = ToolRegistry()
    result = load_local_plugins(workspace, ["boom", "good"], registry)
    assert result.loaded == ["good"]
    assert [name for name, _ in result.failed] == ["boom"]


def test_invalid_plugin_name_rejected(workspace):
    registry = ToolRegistry()
    result = load_local_plugins(workspace, ["../evil"], registry)
    assert result.loaded == []
    assert result.failed


def test_load_plugins_reads_config(workspace):
    from daemon.config import Config

    _plugin_module(workspace, "cfg_tools", """
def register(registry):
    pass
""")
    config = Config({"plugins": {"enabled": ["cfg_tools"]}},
                    user_path=None, project_path=None)
    registry = ToolRegistry()
    result = load_plugins(workspace, config, registry)
    assert result.loaded == ["cfg_tools"]
