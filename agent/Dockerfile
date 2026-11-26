# The lecturer. Built from the top of the repo (see docker-compose.yml):
#   docker build -f agent/Dockerfile .
# Lessons, pictures made in class and logs are folders mounted at /app/lessons,
# /app/generated and /app/logs. Settings come from the environment (agent/.env.local).
# The shared screen needs a Chrome or Chromium in the image; this one has none, so a
# lesson's `open:` steps are skipped.
FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app/agent
COPY agent/pyproject.toml agent/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --extra openai
COPY agent/ ./
RUN uv sync --locked --no-dev --extra openai
CMD ["uv", "run", "--no-sync", "python", "-m", "lecturer.agent", "start"]
