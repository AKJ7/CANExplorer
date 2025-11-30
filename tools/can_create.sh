#!/usr/bin/env bash
#
# Create a virtual CAN connection

can_interface='can0'

function help() {
cat <<EOF
Usage:
  bash $0 [options]

  Options:
    -I, --interface [INTERFACE] CAN interface to create. Default $can_interface
EOF
}


while (($#)); do
  opt="$1"
  case $opt in
    -h | --help)
      help
      exit 0
      ;;
    -I | --interface)
      can_interface="$2"
      shift 2
      ;;
    *)
      echo -e "> Unknown option: '$opt'\n"
      help
      exit 1
      ;;
  esac
done

command="sudo modprobe vcan && sudo ip link add dev $can_interface type vcan && sudo ip link set $can_interface up"

echo -e "$command\n"
eval "$command"