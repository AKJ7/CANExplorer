"""The J2534 bridge: runs a J2534 driver in its own process, for drivers of another architecture than CANExplorer
(32-bit drivers, which a 64-bit program cannot load). CANExplorer starts it in a 32-bit Python, or as the
j2534-bridge32 program of the installers, and talks to it over stdin and stdout.

Only the standard library is used, so that the bridge runs in a bare 32-bit Python: `python j2534_bridge.py`.

Protocol: one JSON value per line. The bridge starts with {"bridge": PROTOCOL_VERSION, "machine": …}; each request
is [call, arguments …] and is answered by {"result": …} or {"error": "…"}. The first call is ["load", path],
the others are the methods of PassThruDriver, with bytes as hex strings. ["exit"] or the end of stdin ends it.
"""

import json
import sys
from typing import Any, BinaryIO, Optional

if __package__:
    from .j2534_driver import PassThruDriver, load_library, process_machine
else:  # Run as a script (`python j2534_bridge.py`): j2534_driver.py is next to it
    from j2534_driver import PassThruDriver, load_library, process_machine  # type: ignore[no-redef,import-not-found]

PROTOCOL_VERSION = 1
CALLS = {
    'open',
    'connect',
    'start_pass_filter',
    'set_config',
    'ioctl',
    'read_version',
    'last_error',
    'read',
    'write',
    'disconnect',
    'close',
}


def _send(output: BinaryIO, value: Any) -> None:
    output.write(json.dumps(value).encode('utf-8') + b'\n')
    output.flush()


def _handle(driver: Optional[PassThruDriver], call: str, arguments: list) -> Any:
    if driver is None or call not in CALLS:
        raise ValueError(f'Unknown call {call!r}' if driver is not None else 'No driver loaded')
    if call == 'write':
        channel_id, protocol, tx_flags, data, timeout_ms = arguments
        return driver.write(channel_id, protocol, tx_flags, bytes.fromhex(data), timeout_ms)
    result = getattr(driver, call)(*arguments)
    if call == 'read':
        status, messages = result
        return [
            status,
            [[protocol, status_bits, timestamp, data.hex()] for protocol, status_bits, timestamp, data in messages],
        ]
    return result


def serve(requests: BinaryIO, output: BinaryIO) -> None:
    """Answer requests until ["exit"] or the end of input."""
    _send(output, {'bridge': PROTOCOL_VERSION, 'machine': process_machine() or 0})
    driver: Optional[PassThruDriver] = None
    for line in requests:
        try:
            request = json.loads(line)
            call, arguments = request[0], request[1:]
            if call == 'exit':
                _send(output, {'result': None})
                return
            if call == 'load':
                driver = PassThruDriver(load_library(arguments[0]))
                _send(output, {'result': None})
                continue
            _send(output, {'result': _handle(driver, call, arguments)})
        except Exception as e:
            _send(output, {'error': f'{type(e).__name__}: {e}'})


def main() -> None:
    serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == '__main__':
    main()
