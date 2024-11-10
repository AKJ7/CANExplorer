FROM ubuntu:noble

ENV TZ="Europe/Berlin"
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN apt -y update
RUN apt -y install git vim python3 python3-pip 
# pipenv 

RUN apt -y update
RUN apt -y install libglib2.0-0 libglu1-mesa-dev libxkbcommon-x11-0 build-essential libgl1-mesa-dev libdbus-1-dev libxcb-*

RUN useradd -ms /bin/bash can_explorer