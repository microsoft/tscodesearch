# Dockerfile for codesearch indexserver
#
# Builds a container that runs:
#   - Typesense server    (full-text search, port 8108)
#   - Indexserver API     (management + watcher + verifier, port 8109)
#
# The MCP server runs on the host as a Node.js process (mcp_server.js).
#
# Usage:
#   docker build -t codesearch-mcp -f docker/Dockerfile .
#   docker run -d --rm \
#       -p 8109:8109 \
#       -v /path/to/source:/source:ro \
#       -v codesearch_data:/typesensedata \
#       codesearch-mcp

FROM python:3.11-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Environment variables
ENV TYPESENSE_DIR=/opt/typesense
ENV TYPESENSE_DATA=/typesensedata
ENV PYTHONUNBUFFERED=1

# Download and install Typesense binary
ARG TYPESENSE_VERSION=27.1
RUN mkdir -p ${TYPESENSE_DIR} && \
    curl -fsSL "https://dl.typesense.org/releases/${TYPESENSE_VERSION}/typesense-server-${TYPESENSE_VERSION}-linux-amd64.tar.gz" \
    | tar -xz -C ${TYPESENSE_DIR} && \
    chmod +x ${TYPESENSE_DIR}/typesense-server && \
    ln -s ${TYPESENSE_DIR}/typesense-server /usr/local/bin/typesense-server

# Python dependencies
RUN pip install --no-cache-dir \
    tree-sitter \
    tree-sitter-c-sharp \
    tree-sitter-python \
    tree-sitter-rust \
    tree-sitter-javascript \
    tree-sitter-typescript \
    tree-sitter-cpp \
    tree-sitter-sql \
    typesense \
    watchdog \
    pathspec \
    pytest

# Copy application code
COPY . /app/

# Copy and set up entrypoint + e2e helper (convert CRLF to LF for Windows compatibility)
COPY scripts/entrypoint.sh /entrypoint.sh
COPY scripts/e2e.sh /app/scripts/e2e.sh
RUN sed -i 's/\r$//' /entrypoint.sh /app/scripts/e2e.sh \
    && chmod +x /entrypoint.sh /app/scripts/e2e.sh

# Create required directories
RUN mkdir -p ${TYPESENSE_DATA} /source

# Default environment variables (no secrets — api key is generated at runtime or passed via config mount)
ENV CODESEARCH_PORT=8108
ENV CODESEARCH_ROOT_NAME=default
# Volume mount points
VOLUME ["/typesensedata"]

# Expose management API port (Typesense is internal-only)
EXPOSE 8109

ENTRYPOINT ["/entrypoint.sh"]
