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
    gnupg \
    unzip \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list \
    && apt-get update && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Install ChromeDriver
RUN wget -O /tmp/chromedriver.zip https://storage.googleapis.com/chrome-for-testing-compatible/latest/linux64/chromedriver-linux64.zip \
    && unzip /tmp/chromedriver.zip -d /usr/local/bin/ \
    && rm /tmp/chromedriver.zip

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