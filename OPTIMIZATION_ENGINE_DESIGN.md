# Optimization Engine Design for Daemon CLI Agent

## Overview

This document proposes an **Optimization Engine** that integrates into the existing Daemon architecture to reduce token usage, improve tool selection, detect failure loops, maintain verified project memory, and provide optimization reporting. The design reuses existing components wherever possible.

---

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────────┐
│                        AgentRuntime                              │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────┐  ┌────────────────┐  ┌────────────────────┐  │
│  │   Context    │  │   Tool         │  │  Failure Loop      │  │
│  │  Optimizer   │  │  Selector      │  │  Detector          │  │
│  └──────────────┘  └────────────────┘  └────────────────────┘  │
│  ┌────────────────┐  ┌──────────────────────────────────────┐  │
│  │ Verified Memory│  │  Optimization Reporter               │  │
│  └────────────────┘  └──────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 1. Context Optimization

### Goal
- Remove redundant/irrelevant context from the conversation before sending to the model
- Preserve instructions, constraints, and important source information
- Track context before/after optimization

### Implementation
**New file:** `daemon/agent/optimizer.py`

```python
class ContextOptimizer:
    """Optimizes conversation context by removing redundancy while preserving
    critical information (instructions, constraints, source references)."""
    
    def __init__(self, config: Config | None = None):
        self.enabled = config.get("optimization.context.enabled", True) if config else True
        self.preserve_system = True
        self.preserve_tool_specs = True
        self.preserve_recent_turns = 4  # keep N most recent turns intact
    
    def optimize(self, messages: list[Message], tools: list[ToolSpec],
                 context_window: int) -> tuple[list[Message], OptimizationStats]:
        """Returns optimized messages and statistics."""
        
    def _deduplicate_tool_results(self, messages: list[Message]) -> list[Message]:
        """Merge consecutive identical tool results."""
        
    def _summarize_old_turns(self, messages: list[Message]) -> list[Message]:
        """Replace older turns with summaries (reusing compact_conversation logic)."""
        
    def _strip_redundant_metadata(self, messages: list[Message]) -> list[Message]:
        """Remove redundant metadata while keeping content intact."""
```

### Integration Point
- Call from `AgentRuntime._maybe_compact()` before compaction
- Or add as a new step in the run loop before `_stream_once()`

### Statistics Tracked
```python
@dataclass
class ContextOptimizationStats:
    original_tokens: int
    optimized_tokens: int
    tokens_saved: int
    turns_removed: int
    tool_results_merged: int
```

---

## 2. Intelligent Tool Selection

### Goal
- Select relevant tools for each task based on the user's request
- Avoid loading unnecessary tool descriptions into the context
- Never hide security-critical tools or bypass permissions

### Implementation
**Extend:** `daemon/tools/base.py` (add category, task_keywords to Tool class)

```python
class Tool:
    # ... existing fields ...
    category: str = "general"           # e.g., "filesystem", "shell", "git", "codeintel", "web", "memory"
    task_keywords: list[str] = []       # e.g., ["read", "search", "analyze"] for read_file
    security_critical: bool = False     # never hide these
    
    def relevance_score(self, user_task: str) -> float:
        """Score 0-1 based on keyword overlap with user task."""
```

**New class:** `ToolSelector`

```python
class ToolSelector:
    """Selects relevant tools for a given task description."""
    
    def __init__(self, registry: ToolRegistry, config: Config | None = None):
        self.registry = registry
        self.enabled = config.get("optimization.tools.enabled", True) if config else True
        self.min_score = config.get("optimization.tools.min_score", 0.15) if config else 0.15
        self.max_tools = config.get("optimization.tools.max_tools", 20) if config else 20
        self.always_include = {"read_file", "list_dir", "glob", "search_files",  # security-critical
                              "memory_note", "run_tests"}  # always available
    
    def select(self, user_task: str, available_tools: list[Tool]) -> list[Tool]:
        """Return filtered tool list for the current task."""
        
    def _score_tool(self, tool: Tool, user_task: str) -> float:
        """Score tool relevance to task."""
        
    def _ensure_critical_tools(self, selected: list[Tool]) -> list[Tool]:
        """Always include security-critical tools."""
```

### Integration Point
- In `AgentRuntime._build_request()`, filter tools through `ToolSelector`
- Pass user's latest message as task context

### Configuration
```toml
[optimization.tools]
enabled = true
min_score = 0.15
max_tools = 20
```

---

## 3. Failure Loop Detection

### Goal
- Detect repeated failed commands and identical unsuccessful approaches
- Recommend a different approach after repeated failures
- Never silently terminate tasks

### Implementation
**New file:** `daemon/agent/failure_detector.py`

```python
@dataclass
class FailurePattern:
    tool_name: str
    error_signature: str  # normalized error message
    count: int
    first_seen: float
    last_seen: float
    args_fingerprint: str  # hash of normalized arguments
    
@dataclass
class FailureReport:
    detected: bool
    patterns: list[FailurePattern]
    recommendation: str | None
    suggested_tools: list[str]  # alternative tools to try

class FailureLoopDetector:
    """Detects repeated failure patterns in tool execution."""
    
    def __init__(self, config: Config | None = None):
        self.enabled = config.get("optimization.failure_detection.enabled", True) if config else True
        self.max_failures = config.get("optimization.failure_detection.max_failures", 3) if config else 3
        self.window_turns = config.get("optimization.failure_detection.window_turns", 10) if config else 10
        self.patterns: dict[str, FailurePattern] = {}  # key: (tool, error_sig, args_fp)
    
    def record(self, tool_name: str, args: dict, result: ToolResult) -> None:
        """Record a tool result for pattern detection."""
        
    def analyze(self) -> FailureReport:
        """Analyze recorded patterns and return recommendations."""
        
    def _normalize_error(self, error: str) -> str:
        """Extract error signature (first line, strip paths/numbers)."""
        
    def _fingerprint_args(self, args: dict) -> str:
        """Hash arguments for similarity detection."""
        
    def _suggest_alternatives(self, failed_tool: str, task_context: str) -> list[str]:
        """Suggest alternative tools based on the failed tool's category."""
```

### Integration Point
- In `AgentRuntime._execute_one()`, call `detector.record()` after each tool execution
- In `AgentRuntime.run()`, after each step call `detector.analyze()` and inject recommendation into next prompt if patterns detected

### Alternative Tool Suggestions
```python
TOOL_ALTERNATIVES = {
    "edit_file": ["apply_patch", "write_file"],
    "run_command": ["run_tests", "shell"],
    "search_files": ["glob", "find_symbols"],
    "read_file": ["project_map", "list_dir"],
    # ...
}
```

---

## 4. Verified Project Memory

### Goal
- Store confirmed repository facts and useful discoveries
- Track their source and confidence
- Remove or invalidate outdated information when contradicted

### Implementation
**Extend:** `daemon/memory.py` (add new kind="verified_fact", confidence, source_location)

```python
@dataclass
class MemoryEntry:
    # ... existing fields ...
    kind: str = "note"  # now: note | fact | decision | research | preference | verified_fact
    confidence: float = 1.0  # 0.0 - 1.0
    source_location: str = ""  # file:line, commit, URL, etc.
    verified: bool = False     # explicitly confirmed by user or test
    supersedes: list[str] = field(default_factory=list)  # IDs of entries this replaces
```

**New methods in MemoryStore:**

```python
class MemoryStore:
    # ... existing methods ...
    
    def add_verified(self, content: str, *, source: str, source_location: str,
                     confidence: float = 0.9, tags: Iterable[str] = (),
                     supersedes: list[str] = ()) -> MemoryEntry:
        """Add a verified fact with source tracking."""
        
    def invalidate(self, entry_id: str, reason: str) -> bool:
        """Mark an entry as outdated/contradicted."""
        
    def find_contradictions(self, new_content: str, kind: str = "verified_fact"
                           ) -> list[MemoryEntry]:
        """Find existing entries that contradict the new content."""
        
    def get_verified_facts(self, limit: int = 20) -> list[MemoryEntry]:
        """Get high-confidence verified facts for context injection."""
```

### Integration Point
- Add `verified_facts` block to system prompt (in `AgentRuntime.system_prompt()`)
- Provide `memory_verify` tool for explicit verification
- Auto-invalidate when file content changes (hook into checkpoints/git)

---

## 5. Optimization Reporting

### Goal
- Record tokens, tool calls, retries, and estimated costs
- Compare optimization-enabled vs optimization-disabled runs
- Report actual measured savings without fabricated percentages

### Implementation
**New file:** `daemon/agent/reporting.py`

```python
@dataclass
class OptimizationReport:
    session_id: str
    model: str
    provider: str
    optimization_enabled: bool
    
    # Token metrics
    total_input_tokens: int
    total_output_tokens: int
    context_optimization_savings: int  # tokens saved by context optimizer
    tool_selection_savings: int        # tokens saved by tool selector
    
    # Tool metrics
    total_tool_calls: int
    failed_tool_calls: int
    retries: int
    failure_loops_detected: int
    
    # Cost metrics
    estimated_cost_usd: float | None
    
    # Memory metrics
    verified_facts_used: int
    memory_entries_added: int
    
    # Timing
    total_duration_s: float
    optimization_overhead_s: float

class OptimizationReporter:
    """Collects and reports optimization metrics."""
    
    def __init__(self, session: Session, config: Config | None = None):
        self.session = session
        self.enabled = config.get("optimization.reporting.enabled", True) if config else True
        self.start_time = time.time()
        self.metrics = OptimizationMetrics()
    
    def record_context_optimization(self, stats: ContextOptimizationStats) -> None:
        """Record context optimization savings."""
        
    def record_tool_selection(self, original_count: int, selected_count: int,
                              tool_tokens_estimate: int) -> None:
        """Record tool selection savings."""
        
    def record_failure_loop(self, report: FailureReport) -> None:
        """Record detected failure loop."""
        
    def record_verified_fact_usage(self, count: int) -> None:
        """Record verified facts injected into context."""
        
    def finalize(self) -> OptimizationReport:
        """Generate final report."""
        
    def print_summary(self) -> None:
        """Print human-readable summary to UI."""
```

### Comparison Mode
- Run with `--optimization-off` flag to collect baseline
- Compare reports using `daemon optimize compare <session_id> <baseline_session_id>`

### Configuration
```toml
[optimization]
enabled = true

[optimization.context]
enabled = true

[optimization.tools]
enabled = true
min_score = 0.15
max_tools = 20

[optimization.failure_detection]
enabled = true
max_failures = 3
window_turns = 10

[optimization.memory]
verified_facts_in_prompt = true
max_verified_facts = 10

[optimization.reporting]
enabled = true
```

---

## Integration Architecture

### Modified: `daemon/agent/runtime.py`

```python
class AgentRuntime:
    def __init__(self, ..., config: Config | None = None, ...):
        # ... existing init ...
        
        # Optimization components
        self.config = config
        self.optimizer = ContextOptimizer(config) if config and config.get("optimization.enabled") else None
        self.tool_selector = ToolSelector(self.registry, config) if config and config.get("optimization.enabled") else None
        self.failure_detector = FailureLoopDetector(config) if config and config.get("optimization.enabled") else None
        self.reporter = OptimizationReporter(self.session, config) if config and config.get("optimization.enabled") else None
        
        # Track optimization state
        self._optimization_enabled = config.get("optimization.enabled", True) if config else True
    
    async def run(self, user_text: str) -> RunResult:
        # ... existing code ...
        
        # Before each stream, optimize context
        if self.optimizer:
            original_tokens = conversation_tokens(self.session.messages, tools)
            self.session.messages, opt_stats = self.optimizer.optimize(
                self.session.messages, tools, self.model.capabilities.context_window)
            self.reporter.record_context_optimization(opt_stats)
        
        # Select relevant tools
        if self.tool_selector:
            selected_tools = self.tool_selector.select(
                user_text or self.session.last_user_text() or "", tools)
            tools = selected_tools
            self.reporter.record_tool_selection(
                len(self.registry.tools()), len(selected_tools), 
                sum(estimate_tokens(t.description) for t in selected_tools))
        
        # ... stream and execute ...
        
        # After tool execution, record for failure detection
        for result in results:
            self.failure_detector.record(call.name, call.arguments, result)
        
        # Check for failure loops
        failure_report = self.failure_detector.analyze()
        if failure_report.detected:
            self.reporter.record_failure_loop(failure_report)
            # Inject recommendation into next turn
            self.session.add_user(f"[Optimization] {failure_report.recommendation}")
        
        # ... rest of loop ...
```

---

## Testing Strategy

### Unit Tests (new files)
- `tests/unit/test_context_optimizer.py`
- `tests/unit/test_tool_selector.py`
- `tests/unit/test_failure_detector.py`
- `tests/unit/test_verified_memory.py`
- `tests/unit/test_optimization_reporting.py`

### Integration Tests
- `tests/integration/test_optimization_engine.py` - full pipeline with mock provider

### Test Coverage
- Context optimizer: deduplication, summarization, token savings measurement
- Tool selector: scoring, critical tool preservation, category filtering
- Failure detector: pattern detection, alternative suggestions
- Verified memory: add/invalidate/contradiction detection
- Reporting: accurate token counting, cost estimation, comparison

---

## Compatibility Requirements

1. **Provider-independent** - works with all existing adapters (OpenAI, Anthropic, Gemini, Ollama, OpenAI-compat)
2. **Configurable toggles** - each feature can be disabled independently
3. **Windows compatible** - no Unix-only dependencies
4. **Preserves security** - never bypasses permissions, never hides security-critical tools
5. **Preserves checkpoints** - all file modifications still checkpointed
6. **Clear logs** - debug-level logging for all optimization decisions

---

## Implementation Order

1. **Context Optimizer** - Core optimization, reuses existing compaction logic
2. **Tool Selector** - Adds task_keywords to tools, filters in `_build_request()`
3. **Failure Loop Detector** - Records in `_execute_one()`, analyzes in run loop
4. **Verified Memory** - Extends MemoryEntry, adds verified_facts to system prompt
5. **Optimization Reporter** - Collects metrics throughout, prints summary at end

Each feature will be implemented incrementally with tests, and the existing test suite will be run after each addition.