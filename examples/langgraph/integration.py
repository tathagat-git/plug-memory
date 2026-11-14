"""
LangGraph integration example — auto-emit events from LangGraph state transitions.

This is a sketch of how to instrument a LangGraph agent so that every node
transition produces a structured event in plug-play-memory.

Requires: pip install langgraph
"""

from typing import TypedDict, Annotated
from operator import add

from plug_play_memory import (
    MemoryStore, PolicyEngine, EventEmitter, Judge,
)


# 1. Initialize plug-play-memory once at app startup
store = MemoryStore("langgraph_memory.db")
policy = PolicyEngine()
policy.set_agent_type("langgraph-1", "coding")
judge = Judge()

emitter = EventEmitter(
    store=store, policy=policy, judge=judge,
    agent_id="langgraph-1", agent_type="coding",
)


# 2. Define your LangGraph state
class AgentState(TypedDict):
    messages: Annotated[list, add]
    current_task: str
    files_modified: list[str]
    last_action: str


# 3. Wrap each node with event emission

def plan_node(state: AgentState) -> AgentState:
    """Planner node — emit a decision event."""
    task = state.get("current_task", "unknown task")
    # ... your planning logic here ...
    plan = "use middleware pattern for auth"  # your planner's output

    # Emit a structured event — gets judged, policy-checked, saved if memorable
    emitter.decision_made(
        topic=task,
        choice=plan,
        reason="dedupe logic across 3 routes",
    )

    return {"last_action": "plan", "messages": [f"Planned: {plan}"]}


def edit_node(state: AgentState) -> AgentState:
    """Code editor node — emit file_edited event."""
    path = "src/auth/middleware.py"
    lines_changed = 47  # your editor's diff stat
    diff_summary = "added RateLimiter class"

    emitter.file_edited(
        path=path,
        diff_summary=diff_summary,
        lines_changed=lines_changed,
    )

    return {
        "last_action": "edit",
        "files_modified": [path],
        "messages": [f"Edited {path}"],
    }


def test_node(state: AgentState) -> AgentState:
    """Test runner node — emit test_run event."""
    passed, failed = 11, 1
    failures = ["test_rate_limit_edge_case: expected 429, got 200"]

    emitter.test_run(
        suite="tests/auth/",
        passed=passed, failed=failed,
        failures=failures,
    )

    return {"last_action": "test", "messages": [f"Tests: {passed} passed, {failed} failed"]}


def deploy_node(state: AgentState) -> AgentState:
    """Deploy node — emit deployed event."""
    emitter.deployed(
        target="staging",
        version="v1.2.3",
        success=True,
        notes="rate limiting live",
    )
    return {"last_action": "deploy", "messages": ["Deployed to staging"]}


# 4. Build the graph (sketch — adapt to your actual LangGraph workflow)
def build_graph():
    try:
        from langgraph.graph import StateGraph, END
    except ImportError:
        print("Install langgraph: pip install langgraph")
        return None

    workflow = StateGraph(AgentState)
    workflow.add_node("plan", plan_node)
    workflow.add_node("edit", edit_node)
    workflow.add_node("test", test_node)
    workflow.add_node("deploy", deploy_node)

    workflow.set_entry_point("plan")
    workflow.add_edge("plan", "edit")
    workflow.add_edge("edit", "test")
    # Conditional: only deploy if tests pass
    workflow.add_conditional_edges(
        "test",
        lambda state: "deploy" if "1 failed" not in state["messages"][-1] else END,
    )
    workflow.add_edge("deploy", END)

    return workflow.compile()


# 5. Recall memory at the start of each session
def load_context_for_session():
    """Call this when a new LangGraph session starts."""
    core_facts = store.get_core("langgraph-1")
    recent = store.recall_recent("langgraph-1", limit=10)

    context = {
        "core_memory": [f.value for f in core_facts],
        "recent_context": [f.value for f in recent],
    }
    return context


if __name__ == "__main__":
    # Demo: run the graph
    graph = build_graph()
    if graph is None:
        exit(1)

    print("Loading context from previous sessions...")
    ctx = load_context_for_session()
    print(f"  Core memory: {ctx['core_memory']}")
    print(f"  Recent context: {len(ctx['recent_context'])} facts")

    print("\nRunning graph...")
    initial_state = {
        "messages": [],
        "current_task": "add rate limiting to auth",
        "files_modified": [],
        "last_action": "",
    }
    final_state = graph.invoke(initial_state)
    print(f"\nFinal state: {final_state}")

    print("\nMemory after run:")
    for f in store.all_facts("langgraph-1"):
        print(f"  [{f.tier}] {f.value}")
