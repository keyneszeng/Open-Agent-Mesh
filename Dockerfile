FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY agents.example.json ./agents.example.json
RUN pip install --no-cache-dir .
RUN useradd -m mesh && mkdir -p /data && chown mesh:mesh /data
USER mesh
ENV MESH_HOST=0.0.0.0 MESH_PORT=8765 MESH_DB_PATH=/data/tasks.sqlite3 MESH_AGENTS_CONFIG=/app/agents.example.json
EXPOSE 8765
CMD ["agentmesh"]
