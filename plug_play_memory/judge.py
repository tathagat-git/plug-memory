"""
Judge for plug-play-memory — brain-inspired, LLM-free by default.

Three layers, evaluated in order:

  1. RulesJudge         — deterministic per-action rules (handles 80% of cases)
  2. HeuristicEnricher  — regex patterns on the raw field (catches recurring,
                          refactor, security, flaky, surprise cases)
  3. SalienceScorer     — 4-factor score (outcome + prediction error + magnitude
                          + schema contradiction) → decides tagged vs consolidated

The brain parallel:
  - Rules ≈ reflex arcs (fast, hardwired)
  - Heuristics ≈ pattern recognition (mid-speed, learned)
  - Salience ≈ amygdala tagging (slow, contextual)
  - tagged flag ≈ synaptic tag (fragile until consolidated)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .events import AgentEvent


@dataclass
class JudgeDecision:
    should_save: bool
    category: str = "transient"  # decision|error|achievement|convention|fact|transient|knowledge_gap
    tier: str = "archival"       # core|recall|archival
    key: str | None = None
    importance: str = "normal"   # low|normal|high
    salience_score: float = 0.5
    summary: str = ""
    tagged: bool = False         # True = needs consolidation before durable
    discard_reason: str = ""


# ---------------------------------------------------------------------------
# Layer 1: deterministic per-action rules
# ---------------------------------------------------------------------------

ACTION_RULES: dict[str, dict[str, JudgeDecision]] = {
    # ---- CODING ----
    "file_edited": {
        "default": JudgeDecision(
            should_save=False, discard_reason="routine file edit"
        ),
        "failure": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="high", key="edit_failure",
            summary="File edit failed",
        ),
    },
    "git_pushed": {
        "default": JudgeDecision(
            should_save=True, category="achievement", tier="recall",
            importance="normal", summary="Pushed to git",
        ),
    },
    "test_run": {
        "success": JudgeDecision(
            should_save=False, discard_reason="passing tests aren't memorable"
        ),
        "failure": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="high", key="test_failure",
            summary="Test run failed",
        ),
        "partial": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="normal", key="test_partial",
            summary="Some tests failed",
        ),
    },
    "decision_made": {
        "default": JudgeDecision(
            should_save=True, category="decision", tier="archival",
            importance="high", summary="Decision made",
        ),
    },
    "error_encountered": {
        "default": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="high", summary="Error encountered",
        ),
    },
    "deployed": {
        "success": JudgeDecision(
            should_save=True, category="achievement", tier="recall",
            importance="normal", summary="Deployed successfully",
        ),
        "failure": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="high", key="deploy_failure",
            summary="Deployment failed",
        ),
    },
    "migration_run": {
        "success": JudgeDecision(
            should_save=False, discard_reason="successful migration is routine"
        ),
        "failure": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="high", key="migration_failure",
            summary="Migration failed",
        ),
    },

    # ---- STUDY ----
    "question_asked": {
        "default": JudgeDecision(
            should_save=False, discard_reason="asking is not memorable"
        ),
    },
    "answer_attempted": {
        "success": JudgeDecision(
            should_save=False, discard_reason="correct answer is routine"
        ),
        "failure": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="high", key="misconception",
            summary="Wrong answer — possible misconception",
        ),
        "partial": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="normal", key="partial_understanding",
            summary="Partial answer — incomplete understanding",
        ),
    },
    "concept_mastered": {
        "default": JudgeDecision(
            should_save=True, category="achievement", tier="recall",
            importance="normal", key="mastery",
            summary="Concept mastered",
        ),
    },
    "knowledge_gap_found": {
        "default": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="high", key="knowledge_gap",
            summary="Knowledge gap identified",
        ),
    },
    "goal_set": {
        "default": JudgeDecision(
            should_save=True, category="fact", tier="core",
            importance="high", key="goal",
            summary="Goal set",
        ),
    },
    "review_scheduled": {
        "default": JudgeDecision(
            should_save=True, category="convention", tier="recall",
            importance="normal", key="review_scheduled",
            summary="Review scheduled",
        ),
    },

    # ---- FINANCIAL ----
    "make_payment": {
        "success": JudgeDecision(
            should_save=True, category="achievement", tier="recall",
            importance="normal", summary="Payment made",
        ),
        "failure": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="high", key="payment_failure",
            summary="Payment failed",
        ),
    },
    "categorize_transaction": {
        "default": JudgeDecision(
            should_save=False, discard_reason="routine categorization"
        ),
    },
    "set_budget_limit": {
        "default": JudgeDecision(
            should_save=True, category="decision", tier="core",
            importance="high", key="budget_limit",
            summary="Budget limit set",
        ),
    },
    "read_account_balance": {
        "default": JudgeDecision(
            should_save=False, discard_reason="balance read is transient"
        ),
    },

    # ---- COMMON ----
    "fact_observed": {
        "default": JudgeDecision(
            should_save=True, category="fact", tier="core",
            importance="normal", summary="Fact observed",
        ),
    },
    "task_completed": {
        "success": JudgeDecision(
            should_save=True, category="achievement", tier="recall",
            importance="normal", summary="Task completed",
        ),
        "failure": JudgeDecision(
            should_save=True, category="error", tier="archival",
            importance="high", key="task_failure",
            summary="Task failed",
        ),
    },
}


class RulesJudge:
    """Layer 1: deterministic rules. Free, instant, predictable."""

    def evaluate(self, event: AgentEvent) -> JudgeDecision:
        action_rules = ACTION_RULES.get(event.action)
        if not action_rules:
            # Unknown action — conservative save with low importance
            return JudgeDecision(
                should_save=True, category="fact", tier="archival",
                importance="low", salience_score=0.4,
                summary=f"{event.action.replace('_', ' ').capitalize()}",
            )

        if event.outcome in action_rules:
            decision = action_rules[event.outcome]
        else:
            decision = action_rules.get(
                "default",
                JudgeDecision(should_save=False, discard_reason="no rule for outcome"),
            )
        # Return a copy so callers can mutate freely
        return JudgeDecision(
            should_save=decision.should_save,
            category=decision.category,
            tier=decision.tier,
            key=decision.key,
            importance=decision.importance,
            salience_score=decision.salience_score,
            summary=decision.summary,
            tagged=decision.tagged,
            discard_reason=decision.discard_reason,
        )


# ---------------------------------------------------------------------------
# Layer 2: heuristic enrichment on the raw field
# ---------------------------------------------------------------------------

HEURISTIC_PATTERNS: list[tuple[Any, dict[str, Any]]] = [
    # Recurring / repeating — escalate importance (prediction error over time)
    (re.compile(r"\b(recurring|again|repeated|persistent|keeps failing|still failing)\b", re.I),
     {"importance": "high", "category": "error", "should_save": True}),
    # Refactor / rewrite — save as decision
    (re.compile(r"\b(refactor|rewrote|restructur|overhaul)\b", re.I),
     {"should_save": True, "category": "decision", "tier": "archival", "importance": "high"}),
    # Flaky / intermittent — downgrade to convention
    (re.compile(r"\b(flaky|intermittent|random fail|sometimes fails)\b", re.I),
     {"category": "convention", "importance": "low"}),
    # Security keywords — always save high
    (re.compile(r"\b(security|vulnerab|CVE|exploit|inject|breach|leak)\b", re.I),
     {"should_save": True, "category": "error", "importance": "high"}),
    # Recurring misconception (study)
    (re.compile(r"\b(misconception|confuses|confused|mixes up|keeps getting wrong)\b", re.I),
     {"should_save": True, "category": "error", "importance": "high", "tier": "archival"}),
]


class HeuristicEnricher:
    """Layer 2: regex patterns on raw text. Catches what rules miss."""

    def enrich(self, decision: JudgeDecision, event: AgentEvent) -> JudgeDecision:
        text = event.raw or ""
        for pattern, overrides in HEURISTIC_PATTERNS:
            if pattern.search(text):
                for k, v in overrides.items():
                    setattr(decision, k, v)
        # Magnitude heuristics on structured fields
        lines_changed = event.inputs.get("lines_changed", 0) or 0
        if lines_changed > 100:
            decision.should_save = True
            decision.importance = "high"
            decision.category = "decision"
        elif lines_changed > 30 and not decision.should_save:
            decision.should_save = True
            decision.importance = "normal"
            decision.category = "decision"
        # Amount-based escalation (financial)
        amount = event.inputs.get("amount", 0) or 0
        if amount > 5000:
            decision.importance = "high"
        return decision


# ---------------------------------------------------------------------------
# Layer 3: salience scoring (amygdala tag)
# ---------------------------------------------------------------------------

class SalienceScorer:
    """
    4-factor salience score, brain-inspired:
      1. Outcome severity (emotional salience)
      2. Prediction error (novelty / surprise)
      3. Magnitude (size of effect)
      4. Schema fit (does this contradict existing memory?)
    Returns score 0.0-1.0. Above threshold → consolidate; below → tag (synaptic).
    """

    def __init__(self, history_lookup: Any = None):
        # history_lookup(agent_id, action) -> count of past successes
        self.history_lookup = history_lookup

    def score(self, event: AgentEvent, decision: JudgeDecision) -> tuple[float, bool]:
        s = 0.0

        # 1. Outcome severity
        if event.outcome == "failure":
            s += 0.4
        elif event.outcome == "partial":
            s += 0.2
        # success adds 0 (routine)

        # 2. Prediction error — if this action usually succeeds, failure is surprising
        if self.history_lookup and event.outcome == "failure":
            past_successes = self.history_lookup(event.agent_id, event.action)
            if past_successes > 5:
                s += 0.3  # expected to succeed but failed = high surprise
            elif past_successes == 0:
                s += 0.1  # first time failing

        # 3. Magnitude
        magnitude = 0
        magnitude += (event.inputs.get("lines_changed", 0) or 0) / 100
        magnitude += (event.inputs.get("amount", 0) or 0) / 1000
        # severity may be a string ("high"/"normal"/"low") — normalize
        sev = event.inputs.get("severity", 0) or 0
        if isinstance(sev, str):
            sev_map = {"high": 10, "normal": 5, "low": 2}
            sev = sev_map.get(sev.lower(), 0)
        magnitude += sev / 10
        if magnitude > 1:
            s += 0.2

        # 4. Decision/category already indicates importance
        if decision.category in ("decision", "error", "knowledge_gap"):
            s += 0.2
        if decision.importance == "high":
            s += 0.1

        s = min(s, 1.0)
        # Tag if score is in the uncertain band — fragile until consolidation
        tagged = 0.3 <= s < 0.6 and decision.category not in ("decision", "fact", "knowledge_gap")
        return s, tagged


# ---------------------------------------------------------------------------
# Composite judge
# ---------------------------------------------------------------------------

class Judge:
    """The full judge pipeline: rules → heuristics → salience."""

    VERSION = "v0.3-rules-v1": rules → heuristics → salience."""

    def __init__(self, history_lookup: Any = None):
        self.rules = RulesJudge()
        self.heuristics = HeuristicEnricher()
        self.scorer = SalienceScorer(history_lookup=history_lookup)

    def evaluate(self, event: AgentEvent) -> JudgeDecision:
        # Layer 1
        decision = self.rules.evaluate(event)
        # Layer 2
        decision = self.heuristics.enrich(decision, event)
        # If discarded by rules+heuristics, skip salience scoring
        if not decision.should_save:
            return decision
        # Layer 3
        score, tagged = self.scorer.score(event, decision)
        decision.salience_score = score
        decision.tagged = tagged
        # Enrich summary with structured details
        decision.summary = self._build_summary(event, decision)
        return decision

    def _build_summary(self, event: AgentEvent, decision: JudgeDecision) -> str:
        """Construct a one-line summary from event fields, capped at 100 chars."""
        # If decision has a template summary AND we didn't change category via heuristic,
        # enrich it with structured details. Otherwise build a fresh summary.
        template_summaries = {
            "Decision made", "Error encountered", "Fact observed",
            "File edit failed", "Test run failed", "Some tests failed",
            "Deployment failed", "Migration failed", "Concept mastered",
            "Knowledge gap identified", "Goal set", "Pushed to git",
            "Deployed successfully", "Task completed", "Task failed",
            "Wrong answer — possible misconception", "Partial answer — incomplete understanding",
            "Payment made", "Payment failed", "Budget limit set", "Review scheduled",
        }

        # Build a fresh summary if the decision was escalated from "discard" by heuristics
        # (in which case the original summary was empty or a discard reason)
        if not decision.summary or decision.summary not in template_summaries:
            # Build from scratch — useful when heuristics escalated a "discard" to "save"
            parts = []
            cat_label = decision.category.replace("_", " ").capitalize() if decision.category != "transient" else "Event"
            parts.append(cat_label)
            topic = event.inputs.get("topic") or event.outputs.get("topic")
            path = event.inputs.get("path")
            branch = event.inputs.get("branch")
            choice = event.outputs.get("choice")
            reason = event.outputs.get("reason")
            misconception = event.outputs.get("misconception")
            target = event.inputs.get("target")
            amount = event.inputs.get("amount")
            diff = event.outputs.get("diff_summary") or event.outputs.get("notes") or event.outputs.get("error")
            if topic:
                parts.append(f"({topic})")
            if path:
                parts.append(f"[{path}]")
            if branch:
                parts.append(f"branch={branch}")
            if choice:
                parts.append(f"→ {choice}")
            if misconception:
                parts.append(f"— {misconception}")
            if reason:
                parts.append(f"({str(reason)[:50]})")
            if target:
                parts.append(f"to {target}")
            if amount:
                parts.append(f"₹{amount}")
            if diff and not (topic or choice or misconception):
                parts.append(f"— {str(diff)[:60]}")
            return " ".join(parts)[:100]

        # Enrich the existing template summary
        parts = [decision.summary.rstrip(".")]
        topic = event.inputs.get("topic") or event.outputs.get("topic")
        path = event.inputs.get("path")
        branch = event.inputs.get("branch")
        choice = event.outputs.get("choice")
        reason = event.outputs.get("reason")
        misconception = event.outputs.get("misconception")
        target = event.inputs.get("target")
        amount = event.inputs.get("amount")
        if topic:
            parts.append(f"({topic})")
        if path:
            parts.append(f"[{path}]")
        if branch:
            parts.append(f"branch={branch}")
        if choice:
            parts.append(f"→ {choice}")
        if misconception:
            parts.append(f"— {misconception}")
        if reason:
            parts.append(f"({str(reason)[:60]})")
        if target:
            parts.append(f"to {target}")
        if amount:
            parts.append(f"₹{amount}")
        return " ".join(parts)[:100]
