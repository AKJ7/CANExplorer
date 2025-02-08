FROM ubuntu:noble

# There is this ugly bug caused by the fact that on Ubuntu 24.xx there
# is already a default user called ubuntu, located with id 1000 and 
# group id 1000. Forwarding a new docker user inside the container
# is rejected as the local group and user are allocated to 1000.
# The trick is to shift the default-created ubuntu user inside the 
# container to another id and group 
# See: https://github.com/microsoft/vscode-remote-release/issues/10030
RUN usermod -u 2000 ubuntu && groupmod -g 2000 ubuntu
ARG DOCKER_USER=can_explorer

ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8
ENV TZ="Europe/Berlin"
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN apt -y update
RUN apt -y install sudo git vim curl python3 python3-pip python3-poetry iproute2 can-utils

RUN apt -y update
RUN apt -y install libglib2.0-0 libglu1-mesa-dev libxkbcommon-x11-0 build-essential libgl1-mesa-dev libdbus-1-dev libxcb-*

# CAN stuffs
RUN apt -y install can-utils

RUN useradd -ms /bin/bash $DOCKER_USER
RUN usermod -aG sudo $DOCKER_USER && echo "${DOCKER_USER} ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/${DOCKER_USER}
RUN chmod 0440 /etc/sudoers.d/${DOCKER_USER}
USER $DOCKER_USER
ENTRYPOINT bash -c "poetry install"