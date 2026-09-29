# syntax=docker/dockerfile:1
# Market Pulse webapp — dashboard + SQLite + MCP for nebulaONE
FROM python:3.12-slim-bookworm

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEBAPP_HOST=0.0.0.0 \
    WEBAPP_PORT=8790 \
    DATA_DIR=/tmp/mpulse-data \
    PERSIST_DIR=/persist \
    TZ=Etc/UTC

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /tmp/mpulse-data /persist

EXPOSE 8790

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8790"]
