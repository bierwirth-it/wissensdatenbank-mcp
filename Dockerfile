# wissensdatenbank-mcp: hybrid search over a Markdown/Obsidian vault as an MCP server
FROM python:3.12-slim

LABEL org.opencontainers.image.title="wissensdatenbank-mcp" \
      org.opencontainers.image.description="MCP server for hybrid search over a Markdown/Obsidian vault in Git, with per-account access control" \
      org.opencontainers.image.source="https://github.com/bierwirth-it/wissensdatenbank-mcp" \
      org.opencontainers.image.licenses="MIT"

RUN apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /opt

# CPU-only torch; the default wheel pulls several GB of CUDA libraries
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt /opt/requirements.txt
RUN pip install --no-cache-dir -r /opt/requirements.txt

# the repository volume may belong to another user (local tests, volume moves)
RUN git config --global --add safe.directory '*'

COPY app/ /opt/app/
# administration CLI: docker exec <container> wdb status
RUN printf '#!/bin/sh\nexec python /opt/app/wdb.py "$@"\n' > /usr/local/bin/wdb && chmod +x /usr/local/bin/wdb

ENV PYTHONUNBUFFERED=1 \
    HF_HOME=/models \
    DATA_DIR=/data \
    INDEX_DIR=/index \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

VOLUME ["/data", "/index", "/models"]
EXPOSE 8000

# the first start downloads the models (~3 GB) and builds the index
HEALTHCHECK --interval=30s --timeout=5s --start-period=1800s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status==200 else 1)"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/opt/app/server.py"]
