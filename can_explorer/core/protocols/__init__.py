from typing import Callable, Dict, Optional

from can_explorer.core.decoding import ProtocolDecoder
from can_explorer.core.protocols.canopen import CanOpenDecoder
from can_explorer.core.protocols.isotp import IsoTpDecoder
from can_explorer.core.protocols.j1587 import J1587Decoder
from can_explorer.core.protocols.j1939 import J1939Decoder, J1939_22Decoder
from can_explorer.core.protocols.kwp2000 import Kwp2000Decoder
from can_explorer.core.protocols.nm import NmDecoder
from can_explorer.core.protocols.nmea2000 import NmeaDecoder
from can_explorer.core.lin import LinDecoder
from can_explorer.core.secoc import SecOcDecoder
from can_explorer.core.uds import UdsDecoder

PROTOCOL_DECODERS: Dict[str, Optional[Callable[[], ProtocolDecoder]]] = {
    'Raw': None,
    IsoTpDecoder.name: IsoTpDecoder,
    UdsDecoder.name: UdsDecoder,
    Kwp2000Decoder.name: Kwp2000Decoder,
    J1939Decoder.name: J1939Decoder,
    J1939_22Decoder.name: J1939_22Decoder,
    NmeaDecoder.name: NmeaDecoder,
    CanOpenDecoder.name: CanOpenDecoder,
    LinDecoder.name: LinDecoder,
    J1587Decoder.name: J1587Decoder,
    NmDecoder.name: NmDecoder,
    SecOcDecoder.name: SecOcDecoder,
}


def create_protocol_decoder(name: str) -> Optional[ProtocolDecoder]:
    try:
        factory = PROTOCOL_DECODERS[name]
    except KeyError:
        raise ValueError(f'Unknown protocol: {name!r}. Known: {", ".join(PROTOCOL_DECODERS)}') from None
    return factory() if factory is not None else None
