FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

RUN pip install --no-cache-dir uv

COPY requirements.txt pyproject.toml ./
RUN uv pip install --system -r requirements.txt
# The tokenizers py-readability-metrics needs for the RFP intake metrics, in a
# directory NLTK searches by default.
RUN python -m nltk.downloader -d /usr/local/share/nltk_data punkt punkt_tab

COPY services ./services
COPY scripts ./scripts
COPY packages/shared ./packages/shared
COPY data/__init__.py ./data/__init__.py
COPY data/pipelines ./data/pipelines
COPY data/process ./data/process
# The customer orders the agent's harness checks (SEC-114, AGENT_ORDERS_PATH).
COPY data/raw/incidents-trackflow.csv ./data/raw/incidents-trackflow.csv
# The MCP server (the `mcp` Compose service runs it from this same image).
COPY mcps ./mcps
RUN uv pip install --system --no-deps --editable .

EXPOSE 8000
CMD ["uvicorn", "services.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--reload-dir", "/app/services", "--reload-dir", "/app/packages/shared/trackflow_shared"]