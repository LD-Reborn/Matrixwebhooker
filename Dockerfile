FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Per-bot runtime state (token/room caches) lives in /app/state and must be
# writable by the unprivileged runtime user. (Creating it in the image also
# makes Docker's named-volume copy carry the right ownership.)
RUN mkdir -p /app/state && chown 65534:65534 /app/state

# Run as unprivileged user (uid 65534)
USER 65534

EXPOSE 8080

CMD ["python", "server.py"]
