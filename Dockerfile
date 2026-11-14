# plug-play-memory Dockerfile — security-hardened, non-root, correct volumes
#
# Usage:
#   docker build -t plug-play-memory .
#   docker run -d --name agentmemory \
#     -v $(pwd)/data:/app/data \
#     -v $(pwd)/policies/custom:/app/policies/custom \
#     -v $(pwd)/config.yaml:/app/config.yaml:ro \
#     -e AGENTMEMORY_AGENT_ID=coder-1 \
#     -e AGENTMEMORY_AGENT_TYPE=coding \
#     -e AGENTMEMORY_DB_KEY="your-encryption-key" \
#     -e AGENTMEMORY_AUDIT_KEY="your-audit-signing-key" \
#     plug-play-memory

FROM python:3.12-slim

LABEL org.opencontainers.image.title="plug-play-memory"
LABEL org.opencontainers.image.description="Self-hosted, brain-inspired memory + permissions for AI agents"
LABEL org.opencontainers.image.source="https://github.com/tathagat-git/plug-play-memory"
LABEL org.opencontainers.image.licenses="Apache-2.0"

# Don't write .pyc files, don't buffer stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY plug_play_memory/ ./plug_play_memory/

# Install the package
RUN pip install -e .

# Create non-root user (M6 fix)
RUN groupadd -r agentmemory && \
    useradd -r -g agentmemory -d /app -s /sbin/nologin -u 1000 agentmemory

# Create data directory owned by non-root user
RUN mkdir -p /app/data /app/policies/custom && \
    chown -R agentmemory:agentmemory /app

# Default config
RUN echo 'db_path: /app/data/memory.db\n\
auth:\n\
  enabled: true\n\
  default_agent_id: local\n\
  default_agent_type: coding\n\
  default_scopes:\n\
    - memory:read\n\
    - memory:write\n\
    - memory:delete\n\
    - permission:check\n\
    - admin:consolidate\n\
    - admin:audit' > /app/config.yaml && \
    chown agentmemory:agentmemory /app/config.yaml

# Switch to non-root user (M6 fix)
USER agentmemory

# Persist data directory (M7 fix — directory, not file)
VOLUME ["/app/data", "/app/policies/custom"]

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD agentmemory doctor || exit 1

# Default entrypoint: run the MCP server on stdio
ENTRYPOINT ["agentmemory"]
CMD ["serve", "--db", "/app/data/memory.db", "--config", "/app/config.yaml", "--policies", "/app/policies/custom"]
