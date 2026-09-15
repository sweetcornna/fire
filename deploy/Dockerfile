# The router runs OpenWrt (musl), so the flow lives in a glibc container.
FROM mcr.microsoft.com/playwright/python:v1.58.0-noble

WORKDIR /app

# Mainland hosts want a domestic mirror; overseas ones are faster on PyPI.
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages \
        -i "$PIP_INDEX_URL" -r requirements.txt

COPY . .

# core/browser.py points PLAYWRIGHT_BROWSERS_PATH at <repo>/chrome outside CI,
# so the browser has to live there - and the copied directory holds a macOS
# build from the developer's machine.
RUN rm -rf /app/chrome \
 && PLAYWRIGHT_BROWSERS_PATH=/app/chrome playwright install chromium

# 1GB of RAM with the browser sharing it: keep Chromium off /dev/shm.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEADLESS=1

CMD ["python", "main.py"]
