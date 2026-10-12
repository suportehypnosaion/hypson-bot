# PSike Meet — Host Auto-Join Service
#
# The image needs REAL Google Chrome (not Playwright's bundled
# Chromium — Google blocks sign-in on the automation build), so we
# install it from Google's apt repo. Headless mode means no Xvfb /
# display server is required.

FROM python:3.12-slim

# ─── Google Chrome ─────────────────────────────────────────────
RUN apt-get update \
    && apt-get install -y --no-install-recommends wget gnupg ca-certificates tzdata \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub \
       | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
    # Record the installed Chrome version in the build log (SOUP / US-039).
    # google-chrome-stable comes from a rolling apt repo, so the exact version
    # is captured here per build rather than hard-pinned (Google removes old
    # versions, which would break rebuilds). Record this in ../docs/SOUP.md.
    && google-chrome-stable --version \
    && rm -rf /var/lib/apt/lists/*

# ─── Python deps ───────────────────────────────────────────────
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── App code ──────────────────────────────────────────────────
COPY bot/ ./bot/
COPY scripts/ ./scripts/

# DOCKER=1 makes the joiner add --no-sandbox / --disable-dev-shm-usage
# (required for Chrome inside containers).
ENV DOCKER=1 \
    PYTHONUNBUFFERED=1 \
    BROWSER_PROFILE_DIR=/profile/browser-profile \
    TRACKER_DB_PATH=/data/bot-state.sqlite

# Non-root user; /profile holds the signed-in Chrome profile (mounted),
# /data holds the SQLite tracker (named volume).
# HOST_UID must match the owner of the mounted browser-profile/ on the
# host (run `id -u` there), or the container can't read the cookies.
ARG HOST_UID=1000
RUN useradd -m -u ${HOST_UID} botuser \
    && mkdir -p /data /profile \
    && chown -R botuser:botuser /app /data /profile
USER botuser

CMD ["python", "-m", "bot.main"]
