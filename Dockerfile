FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    NOWHERE_HOME=/data

COPY . .

RUN pip install --no-cache-dir . \
    && mkdir -p /data

EXPOSE 8080
VOLUME ["/data"]

CMD ["python", "-m", "nowhere.server", "--http"]
