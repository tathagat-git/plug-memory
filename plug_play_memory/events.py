"""Structured events — the unit of memory input."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentEvent:
    """A structured thing an agent did. This is what the judge evaluates."""
    agent_id: str
    agent_type: str          # "coding" | "study" | "financial" | ...
    action: str              # "file_edited" | "git_pushed" | "answer_attempted" | ...
    inputs: dict[str, Any] = field(default_factory=dict)
    outputs: dict[str, Any] = field(default_factory=dict)
    outcome: str = "success"  # "success" | "failure" | "partial"
    raw: str = ""
    timestamp: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_prompt(self) -> str:
        lines = [
            f"AGENT_TYPE: {self.agent_type}",
            f"ACTION: {self.action}",
            f"OUTCOME: {self.outcome}",
        ]
        if self.inputs:
            lines.append(f"INPUTS: {self.inputs}")
        if self.outputs:
            lines.append(f"OUTPUTS: {self.outputs}")
        if self.raw:
            lines.append(f"RAW: {self.raw[:300]}")
        return "\n".join(lines)
