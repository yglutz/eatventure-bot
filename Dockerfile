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

FROM python:3.13-slim-bookworm AS scrcpy

# Official Genymobile Linux x86_64 release, pinned to its published SHA-256.
ARG SCRCPY_VERSION=3.3.4
ARG SCRCPY_SHA256=0305d98c06178c67e12427bbf340c436d0d58c9e2a39bf9ffbbf8f54d7ef95a5
RUN test "$(dpkg --print-architecture)" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl --fail --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 30 --max-time 180 --retry 3 --retry-delay 2 \
        "https://github.com/Genymobile/scrcpy/releases/download/v${SCRCPY_VERSION}/scrcpy-linux-x86_64-v${SCRCPY_VERSION}.tar.gz" \
        -o /tmp/scrcpy.tar.gz \
    && printf '%s  %s\n' "${SCRCPY_SHA256}" /tmp/scrcpy.tar.gz | sha256sum -c - \
    && mkdir -p /opt/scrcpy \
    && tar -xzf /tmp/scrcpy.tar.gz -C /opt/scrcpy --strip-components=1 \
    && test -x /opt/scrcpy/scrcpy \
    && test -s /opt/scrcpy/scrcpy-server \
    && rm /tmp/scrcpy.tar.gz \
    && rm -rf /var/lib/apt/lists/*

FROM python:3.13-slim-bookworm AS runtime

COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=platform-tools /opt/platform-tools /opt/platform-tools
COPY --from=scrcpy /opt/scrcpy /opt/scrcpy

ENV PATH="/opt/platform-tools:/opt/scrcpy:${PATH}" \
    SCRCPY_SERVER_PATH=/opt/scrcpy/scrcpy-server \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    DISPLAY=:99

# Keep runtime libraries on stable Bookworm; scrcpy comes from the release stage.
# OpenCV's non-headless wheel needs GL/X11 libraries even for offline processing.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates libsdl2-2.0-0 libusb-1.0-0 libstdc++6 libgl1 libglib2.0-0 libgomp1 libsm6 libxext6 libxrender1 \
        xvfb openbox xauth x11-utils \
    && rm -rf /var/lib/apt/lists/* \
    && scrcpy --version \
    && scrcpy --version | grep -F 'scrcpy 3.3.4' \
    && scrcpy --help > /tmp/scrcpy-help.txt \
    && for option in --video-bit-rate --no-audio --render-driver --window-title --max-size --max-fps --no-clipboard-autosync; do \
        grep -F -- "$option" /tmp/scrcpy-help.txt || exit 1; \
    done \
    && grep -F 'software' /tmp/scrcpy-help.txt \
    && rm /tmp/scrcpy-help.txt

WORKDIR /app
COPY pyproject.toml uv.lock ./

# Lightweight OCR/X11 runtime: never install the learning or development groups.
# amd64 only: the Google platform-tools stage deliberately rejects ARM builds.
FROM runtime AS legacy
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-default-groups --no-install-project

# The allowlisted build context excludes credentials and generated artifacts.
COPY . .
RUN mkdir -p /app/models /app/data /app/logs /app/screenshots /root/.android

# All CLI arguments are forwarded, including --backend adb, --adb-serial,
# --adb-package and --adb-connect options. No ADB connection is made by default.
# Run only the environment installed at build time (uv 0.7.12 run uses no-sync).
ENTRYPOINT ["uv", "run", "--frozen", "--offline", "--no-default-groups", "--no-sync", "python", "portal.py"]
CMD ["--host", "0.0.0.0", "--port", "8080"]

# Keep learning last so existing untargeted builds retain simulation/training.
FROM legacy AS learning
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
ENTRYPOINT ["uv", "run", "--frozen", "--offline", "--no-dev", "--no-sync", "python", "main.py"]
CMD ["train", "--simulate", "--steps", "256", "--checkpoints", "models/smoke"]
