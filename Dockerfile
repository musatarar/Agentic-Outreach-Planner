# Agentic Outreach Planner — single-container demo image.
# Matches the project's runtime: Python 3.9, Django dev server, SQLite, committed
# React bundle (no Node needed). `docker compose up` gives a working demo.
FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so the layer caches across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application (see .dockerignore for what's excluded).
COPY . .

EXPOSE 8000

# migrate -> load demo data -> serve on 0.0.0.0 so the port maps out of the container.
ENTRYPOINT ["sh", "docker/entrypoint.sh"]
