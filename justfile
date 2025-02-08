create-vcan:
    sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set vcan0 up

explore:
    python -m can_explorer