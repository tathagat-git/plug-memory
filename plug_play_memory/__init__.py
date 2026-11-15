"""plug-play-memory: self-hosted, brain-inspired memory + permissions for AI agents."""

__version__ = "0.3.0"

from .storage import MemoryStore, Fact
from .audit import AuditLog, AuditEntry
from .policy import PolicyEngine, PolicyDecision
from .events import AgentEvent
from .judge import Judge, JudgeDecision, RulesJudge, HeuristicEnricher, SalienceScorer
from .emitter import EventEmitter
from .consolidation import Consolidator
from .auth import AuthConfig, AuthContext, AuthError, ALL_SCOPES

__all__ = ["MemoryStore","Fact","AuditLog","AuditEntry","PolicyEngine","PolicyDecision","AgentEvent","Judge","JudgeDecision","RulesJudge","HeuristicEnricher","SalienceScorer","EventEmitter","Consolidator","AuthConfig","AuthContext","AuthError","ALL_SCOPES"]
