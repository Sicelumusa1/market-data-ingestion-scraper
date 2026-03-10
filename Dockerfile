# Start with slim Python 3.13 image
FROM python:3.13.10-slim

# Copy uv binary from official uv image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/

# Set working directory
WORKDIR /app

# Add virtual environment to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Install system dependencies for Chrome/ChromeDriver
RUN apt-get update && apt-get install -y \
    wget \
    unzip \
    ca-certificates \
    gnupg \
    gpg \
    --no-install-recommends \
    && wget -q -O - https://dl.google.com/linux/linux_signing_key.pub \
       | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" \
       > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Install ChromeDriver
RUN CHROME_VERSION=$(google-chrome --version | awk '{print $3}') \
    && DRIVER_VERSION=$(wget -qO- https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_${CHROME_VERSION%%.*}) \
    && wget -O /tmp/chromedriver.zip https://storage.googleapis.com/chrome-for-testing-public/${DRIVER_VERSION}/linux64/chromedriver-linux64.zip \
    && unzip /tmp/chromedriver.zip -d /usr/local/bin/ \
    && mv /usr/local/bin/chromedriver-linux64/chromedriver /usr/local/bin/chromedriver \
    && chmod +x /usr/local/bin/chromedriver \
    && rm -rf /tmp/chromedriver.zip /usr/local/bin/chromedriver-linux64

# Copy dependency files
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --locked

# Copy all application scripts
COPY scraper/ ./scraper/
COPY *.py ./

# Create directories for mounted volumes
RUN mkdir -p /app/data /app/archive /app/logs /app/checkpoints

# Set environment variables
ENV SCRAPER_BASE_DIR=/app
ENV SCRAPER_DATA_DIR=/app/data
ENV SCRAPER_ARCHIVE_DIR=/app/archive
ENV SCRAPER_LOGS_DIR=/app/logs
ENV SCRAPER_CHECKPOINT_DIR=/app/checkpoints
ENV PYTHONUNBUFFERED=1

# No ENTRYPOINT - specify in Kestra