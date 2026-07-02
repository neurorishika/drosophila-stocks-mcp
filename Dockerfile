FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/

RUN pip install --no-cache-dir .

ENV MCP_TRANSPORT=streamable-http
ENV MCP_HOST=0.0.0.0

CMD ["python", "-m", "drosophila_stocks_mcp"]
