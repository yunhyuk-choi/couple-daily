# couple-daily — Flask app whose AI mechanism is the `claude` CLI (subprocess).
# The image therefore needs BOTH Python (the app) and Node (to install the CLI).
FROM python:3.12-slim

# --- system deps + Node.js (for the claude CLI) ---
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- the Claude Code CLI (this is the app's AI engine, NOT the Anthropic SDK) ---
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# --- python deps ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- app code ---
COPY . .

ENV FLASK_ENV=production \
    PYTHONUNBUFFERED=1

# The CLI authenticates in prod via a long-lived token supplied as a secret:
#   CLAUDE_CODE_OAUTH_TOKEN  (generate once locally with `claude setup-token`)
# It is injected as an env/secret at runtime — never bake a token into the image.

EXPOSE 8080

# gunicorn serves the app factory. 2 workers; long timeout because `claude -p`
# is an agent and can take several seconds per request.
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "--timeout", "180", "app:app"]
