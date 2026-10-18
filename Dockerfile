# Development image of CANExplorer: Python (through uv), the Qt runtime libraries for headless tests and the Linux
# CAN tools (can-utils, iproute2 for vcan interfaces). See docker-compose.yml for how it is used.
FROM ubuntu:24.04

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Etc/UTC

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git sudo iproute2 can-utils kmod \
        libegl1 libgl1 libglib2.0-0 libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libfontconfig1 libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /usr/local/bin/

# Ubuntu images have a user "ubuntu" with uid 1000; give it another uid so the developer's uid can be used, which
# keeps files created in the mounted source owned by the developer (see
# https://github.com/microsoft/vscode-remote-release/issues/10030)
ARG USER=can_explorer
ARG UID=1000
RUN usermod -u 2000 ubuntu && groupmod -g 2000 ubuntu \
    && useradd --create-home --shell /bin/bash --uid ${UID} ${USER} \
    && echo "${USER} ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/${USER} && chmod 0440 /etc/sudoers.d/${USER}
USER ${USER}

# The virtual environment and uv's cache live outside the mounted source (they are named volumes in
# docker-compose.yml), so the host's .venv is not overwritten with Linux binaries of the container
ENV UV_PROJECT_ENVIRONMENT=/home/${USER}/venv \
    UV_CACHE_DIR=/home/${USER}/uv-cache \
    UV_LINK_MODE=copy \
    QT_QPA_PLATFORM=offscreen
RUN mkdir -p /home/${USER}/venv /home/${USER}/uv-cache

WORKDIR /home/${USER}/CANExplorer
CMD ["bash"]
