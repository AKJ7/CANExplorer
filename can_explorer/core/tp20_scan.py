"""Scanning for VW TP 2.0 modules: a channel setup to each logical address in turn, listing the modules that answer.

For each address the scan sends a channel setup and waits `setup_timeout` (0.1 s by default — a module answers in
milliseconds, so a full scan of 0x01-0xFF takes about half a minute). A module that answers gets the channel's
parameters exchanged and, if asked, a StartDiagnosticSession and a ReadECUIdentification with the option you give
(none is assumed: which option holds what is the manufacturer's); then the channel is closed again. Nothing else
is sent, and no module's state is changed beyond that session, which ends with the channel.

Names come from a label file (`core/diagnostic_labels.py`) when one is given: which logical address is which module
is not built in. For UDS-era vehicles, which do not use TP 2.0, use the UDS discovery (`uds_find_ecus`) instead.
"""

from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional

from can_explorer.core.diagnostic_labels import LabelFile
from can_explorer.core.frames import format_hex
from can_explorer.core.kwp2000_client import KwpClient, KwpError
from can_explorer.core.session import BusSession
from can_explorer.core.tp20 import Tp20Client, Tp20Error, Tp20NoAnswer, Tp20Refused

SETUP_TIMEOUT = 0.1
DEFAULT_ADDRESSES = '01-FF'


@dataclass(frozen=True, slots=True)
class ModuleScanResult:
    address: int
    found: bool
    tx_id: Optional[int] = None  # The tester transmits on it
    rx_id: Optional[int] = None  # The module transmits on it
    name: str = ''
    identification: Optional[bytes] = None
    error: str = ''

    @property
    def identification_text(self) -> str:
        if not self.identification:
            return ''
        return bytes(b for b in self.identification if 0x20 <= b < 0x7F).decode('ascii').strip()

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {'address': f'{self.address:02X}'}
        if self.name:
            result['name'] = self.name
        if self.tx_id is not None:
            result['tester_transmits_on'] = f'{self.tx_id:03X}'
            result['module_transmits_on'] = f'{self.rx_id:03X}'
        if self.identification is not None:
            result['identification'] = format_hex(self.identification)
            result['identification_text'] = self.identification_text
        if self.error:
            result['error'] = self.error
        return result


def parse_addresses(text: str) -> List[int]:
    """Logical addresses from hex ranges and values: '01-FF', '01, 17, 56', '01-0F, 17'."""
    addresses: List[int] = []
    for part in str(text).replace(';', ',').split(','):
        part = part.strip()
        if not part:
            continue
        first, dash, last = part.partition('-')
        try:
            low = int(first.strip(), 16)
            high = int(last.strip(), 16) if dash else low
        except ValueError:
            raise ValueError(f'Invalid address range {part!r}; use hex like 01-FF or 17') from None
        if not (0 <= low <= high <= 0xFF):
            raise ValueError(f'Address range {part!r} must be within 00-FF, low to high')
        addresses.extend(a for a in range(low, high + 1) if a not in addresses)
    if not addresses:
        raise ValueError('No addresses to scan')
    return addresses


async def scan_modules(
    session: BusSession,
    addresses: Iterable[int],
    setup_timeout: float = SETUP_TIMEOUT,
    identification: Optional[int] = None,
    diagnostic_session: Optional[int] = None,
    labels: Optional[LabelFile] = None,
    timeout: float = 1.0,
) -> AsyncIterator[ModuleScanResult]:
    """Yield a result for every address (`found` False when nothing answered), in order."""
    for address in addresses:
        name = labels.module_name(address) if labels else ''
        client = Tp20Client(session, address, timeout=timeout, keep_alive=0, setup_timeout=setup_timeout)
        try:
            await client.open()
        except Tp20NoAnswer:
            yield ModuleScanResult(address, False, name=name)
            continue
        except Tp20Refused as e:
            yield ModuleScanResult(address, True, name=name, error=str(e))
            continue
        except Tp20Error as e:  # Answered the setup, but the parameters exchange failed
            yield ModuleScanResult(address, True, name=name, error=str(e))
            continue
        tx_id, rx_id = client.tx_id, client.rx_id
        data, error = None, ''
        try:
            if identification is not None:
                kwp = KwpClient(client, timeout=timeout)
                if diagnostic_session is not None:
                    await kwp.start_diagnostic_session(diagnostic_session)
                data = await kwp.read_ecu_identification(identification)
        except (KwpError, Tp20Error) as e:
            error = str(e)
        finally:
            client.close()  # Synchronous disconnect: never await in a generator's finally (it runs under cancel)
        yield ModuleScanResult(address, True, tx_id, rx_id, name, data, error)
