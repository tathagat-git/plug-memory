"""
Layered policy engine for plug-play-memory — security-hardened version.

Changes from v0.1:
  - Action whitelist per agent_type (C3 fix) — unknown actions default-deny
  - Nested `when` syntax: {amount: {le: 1000}} (M2 fix) — no more suffix collision
  - agent_type passed as parameter to check(), no shared-state mutation (M5 fix)
  - Reserved agent_id validation happens in storage layer (M4 fix)
  - Backwards-compatible with old suffix form (deprecation warning)
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    layers_evaluated: list[str] = field(default_factory=list)


@dataclass
class Rule:
    action: str
    allow: bool
    requires: list[str] = field(default_factory=list)
    when: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyLayer:
    name: str
    source: str
    default_allow: bool
    deny_actions: set[str]
    rules: dict[str, list[Rule]]


# Whitelist of actions allowed per agent_type (C3 fix).
# Any action NOT in this list for the given agent_type is denied at the gate.
ACTION_WHITELIST: dict[str, set[str]] = {
    "coding": {
        "read_memory", "write_memory", "delete_memory", "recall_memory",
        "file_edited", "file_read", "git_pushed", "git_committed",
        "test_run", "build_run", "lint_run", "dependency_installed",
        "deployed", "migration_run",
        # Explicitly denied (in denylist) but listed so policy can evaluate
        "modify_ci_config", "access_production_database",
        "send_email", "make_purchase",
    },
    "study": {
        "read_memory", "write_memory", "delete_memory", "recall_memory",
        "question_asked", "answer_attempted", "concept_mastered",
        "knowledge_gap_found", "goal_set", "review_scheduled",
        "fact_observed",
        "sync_to_flashcard_app", "sync_to_notes_app",
        # Explicitly denied but listed
        "send_email", "make_purchase", "access_internet", "execute_code",
    },
    "financial": {
        "read_memory", "write_memory", "delete_memory", "recall_memory",
        "read_account_balance", "read_transaction_history",
        "categorize_transaction", "generate_budget_report",
        "set_budget_limit", "make_payment", "fact_observed",
        # Explicitly denied but listed
        "share_data_with_third_party", "execute_trade",
    },
    "default": {
        # Minimal set — only memory ops, no side-effecting actions
        "read_memory", "write_memory", "recall_memory",
    },
}

# Absolute denylist from _base — always denied regardless of agent_type
BASE_DENYLIST = {
    "delete_database", "drop_table", "format_disk", "rce",
    "exfiltrate_data", "modify_audit_log", "access_credentials", "modify_secrets",
}


class PolicyEngine:
    BUILTIN_DIR = Path(__file__).parent / "policies" / "builtin"

    def __init__(
        self,
        custom_dir: str | Path | None = None,
        agent_types: dict[str, str] | None = None,
    ):
        self.custom_dir = Path(custom_dir) if custom_dir else None
        self._agent_types = dict(agent_types or {})
        self._layers_cache: dict[str, PolicyLayer] = {}
        self._load_layers()

    def set_agent_type(self, agent_id: str, agent_type: str) -> None:
        """Bind an agent to a built-in policy pack."""
        from .storage import validate_agent_id
        validate_agent_id(agent_id)
        if agent_type not in ACTION_WHITELIST and agent_type != "default":
            warnings.warn(
                f"Unknown agent_type '{agent_type}'. Falling back to 'default' "
                f"whitelist (memory ops only)."
            )
        self._agent_types[agent_id] = agent_type

    def get_agent_type(self, agent_id: str) -> str:
        return self._agent_types.get(agent_id, "default")

    def check(
        self,
        agent_id: str,
        action: str,
        context: dict[str, Any] | None = None,
        agent_type: str | None = None,
    ) -> PolicyDecision:
        """
        Check whether an action is allowed for an agent.

        Args:
            agent_id: The agent's identity
            action: The action to check
            context: Optional context for `when` conditions and `requires`
            agent_type: Override agent_type for this check (M5 fix).
                       If None, uses the bound agent_type.
        """
        from .storage import validate_agent_id
        try:
            validate_agent_id(agent_id)
        except ValueError as e:
            return PolicyDecision(
                allowed=False, reason=f"invalid agent_id: {e}",
                layers_evaluated=[],
            )

        context = context or {}
        if agent_type is None:
            agent_type = self.get_agent_type(agent_id)

        # ─── GATE 0: Action whitelist (C3 fix) ───
        # Any action not in the agent_type's whitelist is denied.
        # This blocks the "lie about category" attack — unknown actions
        # can't reach the policy layer at all.
        whitelist = ACTION_WHITELIST.get(agent_type, ACTION_WHITELIST["default"])
        if action not in whitelist:
            return PolicyDecision(
                allowed=False,
                reason=f"action '{action}' not in whitelist for agent_type '{agent_type}'",
                layers_evaluated=[],
            )

        # ─── GATE 1: Base denylist (always denied) ───
        if action in BASE_DENYLIST:
            return PolicyDecision(
                allowed=False,
                reason=f"absolute deny in builtin:_base",
                layers_evaluated=["builtin:_base"],
            )

        # ─── GATE 2: Layered policy evaluation ───
        layers = self._resolve_layers(agent_id, agent_type)
        evaluated = [l.source for l in layers]

        for layer in layers:
            if action in layer.deny_actions:
                return PolicyDecision(
                    allowed=False,
                    reason=f"absolute deny in {layer.source}",
                    layers_evaluated=evaluated,
                )

        matched_rule: Rule | None = None
        matched_layer: PolicyLayer | None = None
        for layer in layers:
            layer_match: Rule | None = None
            for rule in layer.rules.get(action, []):
                if not self._rule_matches(rule, context):
                    continue
                if rule.allow:
                    missing = [r for r in rule.requires if not context.get(r)]
                    if missing:
                        layer_match = rule  # tentatively
                        continue
                    layer_match = rule
                    break
                else:
                    layer_match = rule
                    break
            if layer_match is not None:
                matched_rule = layer_match
                matched_layer = layer

        if matched_rule is None:
            default_allow = layers[-1].default_allow if layers else False
            return PolicyDecision(
                allowed=default_allow,
                reason=f"no matching rule; default={'allow' if default_allow else 'deny'} ({layers[-1].source if layers else 'none'})",
                layers_evaluated=evaluated,
            )

        if matched_rule.allow:
            missing = [r for r in matched_rule.requires if not context.get(r)]
            if missing:
                return PolicyDecision(
                    allowed=False,
                    reason=f"missing required context: {missing} (from {matched_layer.source})",
                    layers_evaluated=evaluated,
                )

        return PolicyDecision(
            allowed=matched_rule.allow,
            reason=f"matched rule in {matched_layer.source}",
            layers_evaluated=evaluated,
        )

    def list_layers(self) -> list[str]:
        return list(self._layers_cache.keys())

    def list_allowed_actions(self, agent_type: str) -> set[str]:
        """Return the action whitelist for an agent_type (C3 fix)."""
        return ACTION_WHITELIST.get(agent_type, ACTION_WHITELIST["default"]).copy()

    # -- layer resolution ----------------------------------------------------

    def _resolve_layers(self, agent_id: str, agent_type: str) -> list[PolicyLayer]:
        candidates = [
            "builtin:_base",
            f"builtin:{agent_type}",
            f"custom:{agent_type}",
            f"custom:{agent_id}",
        ]
        layers = []
        for source in candidates:
            layer = self._layers_cache.get(source)
            if layer is None:
                continue
            if source.startswith("custom:") and not layer.rules and not layer.deny_actions:
                continue
            layers.append(layer)
        return layers

    def _load_layers(self) -> None:
        self._load_layer("builtin", "_base")
        if self.BUILTIN_DIR.exists():
            for path in sorted(self.BUILTIN_DIR.glob("*.yaml")):
                if path.stem != "_base":
                    self._load_layer("builtin", path.stem)
        if self.custom_dir and self.custom_dir.exists():
            for path in sorted(self.custom_dir.glob("*.yaml")):
                self._load_layer("custom", path.stem)

    def _load_layer(self, kind: str, name: str) -> None:
        source = f"{kind}:{name}"
        if kind == "builtin":
            yaml_path = self.BUILTIN_DIR / f"{name}.yaml"
        else:
            yaml_path = self.custom_dir / f"{name}.yaml"
        if not yaml_path.exists():
            return

        spec = yaml.safe_load(yaml_path.read_text()) or {}
        deny_actions: set[str] = set()
        rules: dict[str, list[Rule]] = {}

        for r in spec.get("deny_rules", []) or []:
            deny_actions.add(r["action"])

        for r in spec.get("rules", []) or []:
            action = r.get("action")
            if not action:
                continue
            rule = Rule(
                action=action,
                allow=bool(r.get("allow", False)),
                requires=r.get("requires", []) or [],
                when=r.get("when", {}) or {},
            )
            rules.setdefault(action, []).append(rule)

        self._layers_cache[source] = PolicyLayer(
            name=name, source=source,
            default_allow=(spec.get("default", "deny") == "allow"),
            deny_actions=deny_actions, rules=rules,
        )

    def _rule_matches(self, rule: Rule, context: dict[str, Any]) -> bool:
        """
        `when` conditions support (M2 fix):
          - {key: value}           — equality
          - {key: [v1, v2]}        — value in list
          - {key: {le: N}}         — context[key] <= N  (nested form, PREFERRED)
          - {key_le: N}            — DEPRECATED suffix form, still works but warns
        """
        if not rule.when:
            return True
        for cond_key, expected in rule.when.items():
            # Detect operator suffix (deprecated form — M2 fix)
            for op_suffix in ("_le", "_ge", "_lt", "_gt"):
                if cond_key.endswith(op_suffix):
                    warnings.warn(
                        f"Policy uses deprecated suffix form '{cond_key}'. "
                        f"Use nested form: {{{cond_key[:-3]}: {{{op_suffix[1:]}: {expected}}}}}",
                        DeprecationWarning, stacklevel=3,
                    )
                    ctx_key = cond_key[: -len(op_suffix)]
                    actual = context.get(ctx_key)
                    if isinstance(actual, str) and actual.replace(".", "").replace("-", "").isdigit():
                        try:
                            actual = float(actual)
                        except ValueError:
                            pass
                    op_fn = {
                        "_le": lambda a, b: a is not None and a <= b,
                        "_ge": lambda a, b: a is not None and a >= b,
                        "_lt": lambda a, b: a is not None and a < b,
                        "_gt": lambda a, b: a is not None and a > b,
                    }[op_suffix]
                    if not op_fn(actual, expected):
                        return False
                    break
            else:
                # No operator suffix — check nested dict form first (M2 fix)
                if isinstance(expected, dict):
                    ctx_key = cond_key
                    actual = context.get(ctx_key)
                    if isinstance(actual, str) and actual.replace(".", "").replace("-", "").isdigit():
                        try:
                            actual = float(actual)
                        except ValueError:
                            pass
                    for op, threshold in expected.items():
                        op_fns = {
                            "le": lambda a, b: a is not None and a <= b,
                            "ge": lambda a, b: a is not None and a >= b,
                            "lt": lambda a, b: a is not None and a < b,
                            "gt": lambda a, b: a is not None and a > b,
                        }
                        if op not in op_fns:
                            continue
                        if not op_fns[op](actual, threshold):
                            return False
                elif isinstance(expected, list):
                    if context.get(cond_key) not in expected:
                        return False
                else:
                    if context.get(cond_key) != expected:
                        return False
        return True
