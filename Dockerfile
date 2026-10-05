# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.7.12 AS uv
FROM python:3.13-slim-bookworm AS platform-tools

# Google's Linux platform-tools support Android 11+ pairing; Bookworm's adb does not.
# The official Linux archive contains x86-64 executables, not ARM64 executables.
ARG PLATFORM_TOOLS_VERSION=36.0.2
RUN test "$(dpkg --print-architecture)" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl unzip \
    && curl --fail --location --retry 3 --max-time 180 \
        "https://dl.google.com/android/repository/platform-tools_r${PLATFORM_TOOLS_VERSION}-linux.zip" \
        -o /tmp/platform-tools.zip \
    && unzip -q /tmp/platform-tools.zip -d /opt \
    && chmod +x /opt/platform-tools/adb \
    && /opt/platform-tools/adb version \
    && /opt/platform-tools/adb help 2>&1 | grep -q 'pair HOST'

FROM python:3.13-slim-bookworm

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=platform-tools /opt/platform-tools /opt/platform-tools

ENV PATH="/opt/platform-tools:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# OpenCV's non-headless wheel needs GL/X11 libraries even for offline processing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libusb-1.0-0 libstdc++6 libgl1 libglib2.0-0 libgomp1 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

# The allowlisted build context excludes credentials and generated artifacts.
COPY . .
RUN mkdir -p /app/models /app/data /app/logs /app/screenshots /root/.android

# All CLI arguments are forwarded, including --backend adb, --adb-serial,
# --adb-package and --adb-connect options. No ADB connection is made by default.
ENTRYPOINT ["uv", "run", "--frozen", "--offline", "python", "main.py"]
CMD ["train", "--simulate", "--steps", "256", "--checkpoints", "models/smoke"]
