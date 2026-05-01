# Pinned by digest — see top-level Dockerfile for rationale.
# Refresh: ``docker pull python:3.12-slim && docker inspect --format
# '{{index .RepoDigests 0}}' python:3.12-slim``.
FROM python:3.12-slim@sha256:46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3

# Build-time mirror fallbacks; see top-level Dockerfile for rationale.
ARG APT_MIRROR=""
ARG PIP_INDEX_URL=""
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org|$APT_MIRROR|g; s|http://security.debian.org|$APT_MIRROR|g" \
            /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
        sed -i "s|http://deb.debian.org|$APT_MIRROR|g; s|http://security.debian.org|$APT_MIRROR|g" \
            /etc/apt/sources.list 2>/dev/null || true; \
    fi
ENV PIP_INDEX_URL=${PIP_INDEX_URL:+$PIP_INDEX_URL}

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Default to the bundled Redis service in docker-compose; override as needed.
ENV REDIS_URL=redis://redis:6379/0

# Copy app code
COPY . .

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
