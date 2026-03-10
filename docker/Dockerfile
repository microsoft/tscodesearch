# Dockerfile for codesearch MCP server
#
# Builds a container that runs:
#   - Typesense server (full-text search)
#   - File watcher (incremental index updates)
#   - MCP server (SSE/HTTP interface for Claude)
#
# Usage:
#   docker build -t codesearch-mcp -f docker/Dockerfile .
#   docker run -d --rm \
#       -p 3000:3000 \
#       -v /path/to/source:/source:ro \
#       -v codesearch_data:/typesensedata \
#       codesearch-mcp

FROM python:3.11-slim

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
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
    mcp \
    tree-sitter \
    tree-sitter-c-sharp \
    typesense \
    watchdog \
    pathspec \
    uvicorn

# Copy application code
# Note: Build context should be the codesearch directory
COPY . /app/codesearch/

# Copy and set up entrypoint (convert CRLF to LF for Windows compatibility)
COPY docker/entrypoint.sh /entrypoint.sh
RUN sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh

# Create required directories
RUN mkdir -p ${TYPESENSE_DATA} /source

# Default environment variables
ENV CODESEARCH_PORT=8108
ENV CODESEARCH_ROOT_NAME=default
ENV CODESEARCH_API_KEY=""
ENV MCP_PORT=3000

# Volume mount points
VOLUME ["/source", "/typesensedata"]

# Expose MCP SSE port
EXPOSE 3000

ENTRYPOINT ["/entrypoint.sh"]
