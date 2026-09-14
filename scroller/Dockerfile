# syntax=docker/dockerfile:1
# LinkedIn feed scroller MCP + remote LinkedIn login (Xvfb / noVNC)
FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PROFILES_DIR=/data/profiles \
    DEFAULT_HEADED=false \
    REMOTE_LOGIN_ENABLED=true \
    DEFAULT_LOGIN_WAIT_SECONDS=600 \
    SCROLLER_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    NOVNC_ROOT=/usr/share/novnc \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb \
      x11vnc \
      novnc \
      websockify \
      fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY mcp_server.py ./mcp_server.py

RUN mkdir -p /data/profiles /app/out

EXPOSE 8000

CMD ["python", "mcp_server.py"]
