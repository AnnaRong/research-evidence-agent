FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    REA_STATE_DB=/data/research_agent.sqlite3 \
    REA_FEEDBACK_DIR=/data/feedback \
    REA_ANALYTICS_DIR=/data/analytics \
    EDISON_MCP_COMMAND=edison-mcp \
    EDISON_MCP_ARGS=stdio

WORKDIR /app

RUN pip install --no-cache-dir --index-url https://pypi.org/simple edison-mcp==0.2.7 \
    && mkdir -p /data/feedback /data/analytics

COPY scripts/ ./scripts/
COPY ui/ ./ui/

EXPOSE 8080

CMD ["python3", "ui/server.py"]
