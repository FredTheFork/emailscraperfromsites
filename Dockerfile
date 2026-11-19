FROM python:3.12-slim

ARG WITH_BROWSER=false
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SCRAPER_DB=/data/harvest.db \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
WORKDIR /app

COPY requirements.lock requirements-browser.lock ./
RUN pip install --no-cache-dir -r requirements.lock \
    && if [ "$WITH_BROWSER" = "true" ]; then \
         pip install --no-cache-dir -r requirements-browser.lock \
         && playwright install --with-deps chromium \
         && chmod -R a+rX /ms-playwright; \
       fi
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . \
    && groupadd --system --gid 10001 publicmail \
    && useradd --system --uid 10001 --gid publicmail --home /data publicmail \
    && mkdir -p /data \
    && chown -R publicmail:publicmail /data

USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM
CMD ["publicmail", "serve", "--host", "0.0.0.0", "--port", "8000"]
