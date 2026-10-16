# syntax=docker/dockerfile:1
FROM python:3.11-slim AS deps
WORKDIR /src
COPY pyproject.toml ./
# Install the pinned runtime dependencies from pyproject.toml (single source of versions).
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > requirements.txt \
    && pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8020
RUN groupadd -g 1000 app && useradd -u 1000 -g app -M -s /usr/sbin/nologin app
COPY --from=deps /install /usr/local
WORKDIR /srv
COPY app/ ./app/
COPY registry_docs/ ./registry_docs/
USER 1000:1000
EXPOSE 8020
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.getenv('PORT','8020'), timeout=3)"
CMD ["python", "-m", "app.main"]
