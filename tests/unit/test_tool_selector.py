"""Task-aware tool pruning."""

from daemon.agent.tool_selector import ToolSelector
from daemon.config import Config
from daemon.tools.base import Tool


class FakeTool(Tool):
    def __init__(self, name, description, category="general"):
        self.name = name
        self.description = description
        self.category = category
        self.parameters = {"type": "object", "properties": {}, "required": []}


def _config(**tools):
    payload = {"enabled": True, "min_score": 0.15, "max_tools": 20}
    payload.update(tools)
    return Config({"optimization": {"enabled": True, "tools": payload}},
                  user_path=None, project_path=None)


def _registry():
    tools = [
        FakeTool("read_file", "Read a file with line numbers", "filesystem"),
        FakeTool("write_file", "Write a file", "filesystem"),
        FakeTool("edit_file", "Edit a file", "filesystem"),
        FakeTool("apply_patch", "Apply a unified diff", "filesystem"),
        FakeTool("list_dir", "List a directory", "filesystem"),
        FakeTool("glob", "Find files by pattern", "filesystem"),
        FakeTool("search_files", "Search file contents", "filesystem"),
        FakeTool("file_info", "Metadata for a path", "filesystem"),
        FakeTool("run_command", "Run a shell command", "terminal"),
        FakeTool("run_tests", "Run the project test suite", "code-intelligence"),
        FakeTool("git_status", "Show working tree status", "git"),
        FakeTool("git_diff", "Show a diff", "git"),
        FakeTool("compact_context", "Summarize older history", "context"),
        FakeTool("memory_note", "Store a durable note", "memory"),
    ]
    extras = [
        FakeTool("deploy_k8s", "Roll out a Kubernetes deployment", "infra"),
        FakeTool("render_chart", "Render a chart image", "charts"),
        FakeTool("query_warehouse", "Query the warehouse", "data"),
        FakeTool("send_email", "Send an email", "comms"),
        FakeTool("translate_doc", "Translate a document", "text"),
        FakeTool("ocr_image", "Extract text from an image", "vision"),
        FakeTool("schedule_job", "Schedule a cron job", "infra"),
        FakeTool("provision_vm", "Provision a virtual machine", "infra"),
        FakeTool("train_model", "Train a model", "ml"),
        FakeTool("annotate_dataset", "Annotate a dataset", "ml"),
        FakeTool("generate_pdf", "Generate a PDF report", "docs"),
        FakeTool("sync_crm", "Sync records with the CRM", "comms"),
        FakeTool("design_search", "Search BITON design studies", "design"),
        FakeTool("design_get", "Read one BITON design study and its palette",
                 "design"),
        FakeTool("web_search", "Search the web", "research"),
        FakeTool("web_fetch", "Fetch a web page", "research"),
    ]
    return tools + extras


def test_prunes_unrelated_tools_but_keeps_core():
    selector = ToolSelector(_config(max_tools=20))
    tools = _registry()

    selected = selector.select("fix the off-by-one bug", tools)

    names = {tool.name for tool in selected}
    assert len(selected) < len(tools)
    for core in ("read_file", "edit_file", "run_command", "run_tests",
                 "git_status", "compact_context", "memory_note"):
        assert core in names
    assert "deploy_k8s" not in names
    assert "train_model" not in names
    assert selector.last_stats.tokens_saved > 0


def test_relevant_tools_are_kept():
    selector = ToolSelector(_config(max_tools=15))
    tools = _registry()

    selected = selector.select(
        "build this screen from the biton design study", tools)

    names = {tool.name for tool in selected}
    assert "design_get" in names
    assert "deploy_k8s" not in names


def test_tool_used_in_session_stays_advertised():
    selector = ToolSelector(_config(max_tools=15))
    tools = _registry()
    selector.select("fix the bug", tools)  # first pass prunes web_fetch

    selector.record_use("web_fetch")
    selected = selector.select("fix the bug", tools)

    assert "web_fetch" in {tool.name for tool in selected}


def test_disabled_selects_everything():
    config = Config({"optimization": {"enabled": True,
                                      "tools": {"enabled": False}}},
                    user_path=None, project_path=None)
    selector = ToolSelector(config)
    tools = _registry()

    selected = selector.select("fix the bug", tools)

    assert len(selected) == len(tools)
    assert selector.last_stats.tokens_saved == 0


def test_small_registry_is_left_alone():
    selector = ToolSelector(_config(max_tools=20))
    tools = _registry()[:5]

    selected = selector.select("fix the bug", tools)

    assert selected == tools


def test_global_optimization_toggle_disables_pruning():
    config = Config({"optimization": {"enabled": False,
                                      "tools": {"enabled": True}}},
                    user_path=None, project_path=None)
    assert ToolSelector(config).enabled is False


def test_registry_order_is_preserved():
    selector = ToolSelector(_config(max_tools=15))
    tools = _registry()

    selected = selector.select("fix the bug", tools)
    order = [tool.name for tool in tools]
    positions = [order.index(tool.name) for tool in selected]

    assert positions == sorted(positions)
