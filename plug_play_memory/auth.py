"""
Auth layer for plug-play-memory MCP server — security-hardened.

Changes from v0.1:
  - Tokens stored as SHA-256 hashes (H4 fix) — plaintext tokens never persisted
  - Config schema validation with friendly errors (was KeyError before)
  - Reserved agent_id validation (M4 fix)
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class AuthContext:
    """The identity of the caller, established at connection time."""
    agent_id: str
    agent_type: str
    scopes: list[str]
    token_id: str


ALL_SCOPES = {
    "memory:read",
    "memory:write",
    "memory:delete",
    "permission:check",
    "admin:consolidate",
    "admin:audit",
}


class AuthError(Exception):
    pass


def _hash_token(token: str) -> str:
    """SHA-256 hash of a token. Stored at rest instead of plaintext (H4 fix)."""
    return hashlib.sha256(token.encode()).hexdigest()


class AuthConfig:
    """
    Loads auth config from YAML. Tokens can be specified in two ways:

    1. Plaintext (convenient for first-time setup, but stored as hash internally):
        tokens:
          - id: token-coder-1
            token: sk-coder-1-abc123...

    2. Pre-hashed (recommended for production):
        tokens:
          - id: token-coder-1
            token_hash: 5f2b09c...  # sha256(token)

    Either form works. Plaintext is auto-hashed on load.
    """

    def __init__(self, config_path: str | Path | None = None):
        self.config_path = Path(config_path) if config_path else None
        self.enabled = True
        self.default_agent_id = "local"
        self.default_agent_type = "coding"
        self.default_scopes = ["memory:read", "memory:write", "permission:check"]
        self._tokens: dict[str, dict[str, Any]] = {}  # token_hash -> {id, agent_id, agent_type, scopes}
        if self.config_path and self.config_path.exists():
            self._load()

    def _load(self) -> None:
        try:
            spec = yaml.safe_load(self.config_path.read_text()) or {}
        except yaml.YAMLError as e:
            raise AuthError(f"Invalid YAML in config: {e}")
        auth = spec.get("auth", {})
        self.enabled = auth.get("enabled", True)
        self.default_agent_id = auth.get("default_agent_id", "local")
        self.default_agent_type = auth.get("default_agent_type", "coding")
        self.default_scopes = auth.get("default_scopes",
                                        ["memory:read", "memory:write", "permission:check"])

        # Validate default agent_id isn't reserved (M4 fix)
        from .storage import validate_agent_id, RESERVED_AGENT_IDS
        try:
            validate_agent_id(self.default_agent_id)
        except ValueError as e:
            raise AuthError(
                f"config.yaml: default_agent_id '{self.default_agent_id}' is reserved. "
                f"Reserved: {sorted(RESERVED_AGENT_IDS)}"
            )

        for i, t in enumerate(auth.get("tokens", []) or []):
            # Schema validation (was missing in v0.1)
            if "id" not in t:
                raise AuthError(f"config.yaml: tokens[{i}] missing 'id' field")
            if "agent_id" not in t:
                raise AuthError(f"config.yaml: tokens[{i}] missing 'agent_id' field")
            try:
                validate_agent_id(t["agent_id"])
            except ValueError as e:
                raise AuthError(f"config.yaml: tokens[{i}].agent_id: {e}")

            # Accept either plaintext 'token' or pre-hashed 'token_hash' (H4 fix)
            if "token_hash" in t:
                token_hash = t["token_hash"]
            elif "token" in t:
                token_hash = _hash_token(t["token"])
            else:
                raise AuthError(
                    f"config.yaml: tokens[{i}] must have 'token' or 'token_hash'"
                )
            self._tokens[token_hash] = {
                "id": t["id"],
                "agent_id": t["agent_id"],
                "agent_type": t.get("agent_type", "default"),
                "scopes": t.get("scopes", self.default_scopes),
            }

    def authenticate(self, token: str | None = None) -> AuthContext:
        if not self.enabled:
            return AuthContext(
                agent_id=self.default_agent_id,
                agent_type=self.default_agent_type,
                scopes=list(ALL_SCOPES),
                token_id="disabled",
            )
        if not token:
            return AuthContext(
                agent_id=os.environ.get("AGENTMEMORY_AGENT_ID", self.default_agent_id),
                agent_type=os.environ.get("AGENTMEMORY_AGENT_TYPE",
                                          self.default_agent_type),
                scopes=self.default_scopes,
                token_id="default",
            )
        # Hash the incoming token and compare (H4 fix — constant-time via dict lookup)
        token_hash = _hash_token(token)
        if token_hash not in self._tokens:
            raise AuthError("unknown token")
        t = self._tokens[token_hash]
        return AuthContext(
            agent_id=t["agent_id"],
            agent_type=t["agent_type"],
            scopes=t["scopes"],
            token_id=t["id"],
        )

    @staticmethod
    def has_scope(ctx: AuthContext, scope: str) -> bool:
        return scope in ctx.scopes
