"""Monitoring FlexRay through a Vector interface (VN7600, VN7610, VN7640, VN8900 …) in the XL Driver Library's spy
mode: every frame on the bus is reported as it was seen, with its header and its errors, without configuring a
FlexRay controller of the device or joining the cluster.

EXPERIMENTAL: written from Vector's vxlapi.h (the XL Driver Library header, "Driver Interface Prototypes — customer
version", 2014) and tested against a simulated library, not yet on a device. Used: xlOpenDriver, xlGetDriverConfig
(the channels with FlexRay, XL_BUS_COMPATIBLE_FLEXRAY), xlOpenPort (XL_BUS_TYPE_FLEXRAY, XL_INTERFACE_VERSION_V4),
xlFrActivateSpy (XL_FR_SPY_MODE_ASYNCHRONOUS), xlActivateChannel and xlFrReceive, whose XLfrEvent carries the
XL_FR_SPY_FRAME events: XL_FR_SPY_FRAME_EV with the slot, cycle, header flags, payload and frame errors, and the
channel (XL_FR_CHANNEL_A/B) in flagsChip. Vector documents the extended FlexRay functions as needing its Advanced
FlexRay Library license; whether spy mode is one of them depends on the device.

Unconfirmed: XL_FR_SPY_FRAME_EV.payloadLength is taken in 16-bit words, the unit of the FlexRay header field it
mirrors (vxlapi.h does not say). If payloads show at twice their length, this is the field to check.

Joining the cluster and sending (VectorFlexRayNode), also EXPERIMENTAL and written from vxlapi.h only (its structure
sizes and offsets checked by compiling the header): the FlexRay controller needs the cluster's and the node's full
configuration, XLfrClusterConfig (64 parameters: the cluster's g*/gd* and the node's p*/pd* protocol parameters).
Vector's manual, which gives the units and encodings the driver expects for them (gdMacrotick, pdMicrotick,
baudrate, …), is not public, so this module never derives them from a cluster description: it takes the
configuration the channel already has (xlFrGetChannelConfiguration, when another Vector application configured it,
XL_FR_CHANNEL_CFG_STATUS_VALID_CLUSTER_CFG), or one the user gives as XLfrClusterConfig field values (as Vector's
tools export them). Then xlFrSetConfiguration (with init access), xlFrSetMode (normal; leading coldstart when asked),
xlFrInitStartupAndSync with the key slot's startup/sync frame for a coldstart node, xlActivateChannel; frames are
set up with xlFrTransmit (XL_FR_TX_FRAME, cyclic in their slot, base cycle and repetition). Received frames come as
XL_FR_RX_FRAME, transmitted ones as XL_FR_TXACK_FRAME, symbols as XL_FR_SYMBOL_WINDOW, the POC state as XL_FR_STATUS
and controller errors as XL_FR_ERROR. XL_FR_TX_FRAME_EV.payloadLength is given in 16-bit words, as for the spy
frames (unconfirmed, see above).
"""

import ctypes
import sys
import time
from typing import Any, Dict, List, Optional

from can_explorer.core.flexray import MAX_PAYLOAD, FlexRayError, FlexRayEvent, FlexRayFrame

XL_SUCCESS, XL_ERR_QUEUE_IS_EMPTY, XL_ERR_INIT_ACCESS_MISSING = 0, 10, 158
XL_BUS_TYPE_FLEXRAY = 0x00000004
XL_BUS_COMPATIBLE_FLEXRAY = XL_BUS_TYPE_FLEXRAY
XL_INTERFACE_VERSION_V4 = 4
XL_ACTIVATE_RESET_CLOCK = 8
XL_FR_SPY_MODE_ASYNCHRONOUS = 0x01
XL_FR_SPY_FRAME = 0x008E
XL_FR_RX_FRAME, XL_FR_TX_FRAME, XL_FR_TXACK_FRAME, XL_FR_INVALID_FRAME = 0x0081, 0x0082, 0x0083, 0x0084
XL_FR_SYMBOL_WINDOW, XL_FR_ERROR, XL_FR_STATUS = 0x0086, 0x0087, 0x0088
XL_FR_CHANNEL_CFG_STATUS_VALID_CLUSTER_CFG = 0x04
XL_FR_MODE_NORMAL = 0x00
XL_FR_MODE_NONE, XL_FR_MODE_COLDSTART_LEADING = 0x00, 0x02
XL_FR_TX_MODE_CYCLIC, XL_FR_TX_MODE_NONE = 0x01, 0xFF
XL_FR_PAYLOAD_INCREMENT_NONE = 0
XL_FR_SYMBOL_MTS, XL_FR_SYMBOL_CAS = 0x01, 0x02
XL_FR_CHANNEL_A, XL_FR_CHANNEL_B = 0x01, 0x02
XL_FR_QUEUE_OVERFLOW = 0x0100
XL_FR_MAX_DATA_LENGTH = 254
XL_FR_RX_EVENT_HEADER_SIZE, XL_FR_MAX_EVENT_SIZE = 32, 512
# XL_FR_SPY_FRAME_EV.frameError and the header flags (XL_FR_FRAMEFLAG_*)
SPY_ERRORS = {0x01: 'framing', 0x02: 'header CRC', 0x04: 'frame CRC', 0x08: 'bus'}
FLAG_STARTUP, FLAG_SYNC, FLAG_NULLFRAME, FLAG_PAYLOAD_PREAMBLE = 0x0001, 0x0002, 0x0004, 0x0008
# XL_FR_FRAMEFLAG_* error flags of received frames
FRAME_ERRORS = {0x0200: 'syntax', 0x0400: 'content', 0x0800: 'slot boundary violation', 0x1000: 'TX conflict'}
POC_STATES = {
    0x00: 'default config', 0x01: 'ready', 0x02: 'normal active', 0x03: 'normal passive', 0x04: 'halt',
    0x05: 'monitor mode', 0x0F: 'config', 0x10: 'wakeup standby', 0x11: 'wakeup listen', 0x12: 'wakeup send',
    0x13: 'wakeup detect', 0x20: 'startup prepare', 0x21: 'coldstart listen', 0x22: 'coldstart collision resolution',
    0x23: 'coldstart consistency check', 0x24: 'coldstart gap', 0x25: 'coldstart join',
    0x26: 'integration coldstart check', 0x27: 'integration listen', 0x28: 'integration consistency check',
    0x29: 'initialize schedule', 0x2A: 'abort startup',
}  # fmt: skip
ERROR_TAGS = {
    0x01: 'POC error mode', 0x02: 'sync frames below minimum', 0x03: 'sync frame overload',
    0x04: 'clock correction failure', 0x05: 'NIT failure', 0x06: 'controller error',
}  # fmt: skip

# XLfrClusterConfig, in vxlapi.h's order (all unsigned int), then reserved[15]
CLUSTER_CONFIG_FIELDS = (
    'busGuardianEnable baudrate busGuardianTick externalClockCorrectionMode gColdStartAttempts gListenNoise '
    'gMacroPerCycle gMaxWithoutClockCorrectionFatal gMaxWithoutClockCorrectionPassive gNetworkManagementVectorLength '
    'gNumberOfMinislots gNumberOfStaticSlots gOffsetCorrectionStart gPayloadLengthStatic gSyncNodeMax '
    'gdActionPointOffset gdDynamicSlotIdlePhase gdMacrotick gdMinislot gdMiniSlotActionPointOffset gdNIT '
    'gdStaticSlot gdSymbolWindow gdTSSTransmitter gdWakeupSymbolRxIdle gdWakeupSymbolRxLow gdWakeupSymbolRxWindow '
    'gdWakeupSymbolTxIdle gdWakeupSymbolTxLow pAllowHaltDueToClock pAllowPassiveToActive pChannels '
    'pClusterDriftDamping pDecodingCorrection pDelayCompensationA pDelayCompensationB pExternOffsetCorrection '
    'pExternRateCorrection pKeySlotUsedForStartup pKeySlotUsedForSync pLatestTx pMacroInitialOffsetA '
    'pMacroInitialOffsetB pMaxPayloadLengthDynamic pMicroInitialOffsetA pMicroInitialOffsetB pMicroPerCycle '
    'pMicroPerMacroNom pOffsetCorrectionOut pRateCorrectionOut pSamplesPerMicrotick pSingleSlotEnabled '
    'pWakeupChannel pWakeupPattern pdAcceptedStartupRange pdListenTimeout pdMaxDrift pdMicrotick gdCASRxLowMax '
    'gChannels vExternOffsetControl vExternRateControl pChannelsMTS framePresetData'
).split()


class XL_FR_SPY_FRAME_EV(ctypes.Structure):  # noqa: N801 - the names of vxlapi.h (packed to 8, natural alignment)
    _fields_ = [
        ('frameLength', ctypes.c_uint32),
        ('frameError', ctypes.c_uint8),
        ('tssLength', ctypes.c_uint8),
        ('headerFlags', ctypes.c_uint16),
        ('slotID', ctypes.c_uint16),
        ('headerCRC', ctypes.c_uint16),
        ('payloadLength', ctypes.c_uint8),
        ('cycleCount', ctypes.c_uint8),
        ('reserved', ctypes.c_uint16),
        ('frameCRC', ctypes.c_uint32),
        ('data', ctypes.c_uint8 * XL_FR_MAX_DATA_LENGTH),
    ]


class XL_FR_RX_FRAME_EV(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ('flags', ctypes.c_uint16),
        ('headerCRC', ctypes.c_uint16),
        ('slotID', ctypes.c_uint16),
        ('cycleCount', ctypes.c_uint8),
        ('payloadLength', ctypes.c_uint8),
        ('data', ctypes.c_uint8 * XL_FR_MAX_DATA_LENGTH),
    ]


class XL_FR_TX_FRAME_EV(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ('flags', ctypes.c_uint16),
        ('slotID', ctypes.c_uint16),
        ('offset', ctypes.c_uint8),  # The base cycle
        ('repetition', ctypes.c_uint8),
        ('payloadLength', ctypes.c_uint8),
        ('txMode', ctypes.c_uint8),
        ('incrementSize', ctypes.c_uint8),
        ('incrementOffset', ctypes.c_uint8),
        ('reserved0', ctypes.c_uint8),
        ('reserved1', ctypes.c_uint8),
        ('data', ctypes.c_uint8 * XL_FR_MAX_DATA_LENGTH),
    ]


class XL_FR_SYMBOL_WINDOW_EV(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ('symbol', ctypes.c_uint32),
        ('flags', ctypes.c_uint32),
        ('cycleCount', ctypes.c_uint8),
        ('reserved', ctypes.c_uint8 * 7),
    ]


class XL_FR_STATUS_EV(ctypes.Structure):  # noqa: N801
    _fields_ = [('statusType', ctypes.c_uint32), ('reserved', ctypes.c_uint32)]


class XL_FR_ERROR_EV(ctypes.Structure):  # noqa: N801 - errorInfo (a union of up to 20 bytes) kept raw
    _fields_ = [
        ('tag', ctypes.c_uint8),
        ('cycleCount', ctypes.c_uint8),
        ('reserved', ctypes.c_uint8 * 6),
        ('errorInfo', ctypes.c_uint32 * 5),
    ]


class XLfrClusterConfig(ctypes.Structure):  # noqa: N801 - pack(4), all unsigned int
    _pack_ = 4
    _fields_ = [(name, ctypes.c_uint32) for name in CLUSTER_CONFIG_FIELDS] + [('reserved', ctypes.c_uint32 * 15)]


class XLfrChannelConfig(ctypes.Structure):  # noqa: N801
    _pack_ = 4
    _fields_ = [
        ('status', ctypes.c_uint32),
        ('cfgMode', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32 * 6),
        ('xlFrClusterConfig', XLfrClusterConfig),
    ]


class XLfrMode(ctypes.Structure):  # noqa: N801
    _pack_ = 4
    _fields_ = [
        ('frMode', ctypes.c_uint32),
        ('frStartupAttributes', ctypes.c_uint32),
        ('reserved', ctypes.c_uint32 * 30),
    ]


class _TagData(ctypes.Union):
    _fields_ = [
        ('frRxFrame', XL_FR_RX_FRAME_EV),
        ('frTxFrame', XL_FR_TX_FRAME_EV),
        ('frSymbolWindow', XL_FR_SYMBOL_WINDOW_EV),
        ('frError', XL_FR_ERROR_EV),
        ('frStatus', XL_FR_STATUS_EV),
        ('frSpyFrame', XL_FR_SPY_FRAME_EV),
        ('raw', ctypes.c_uint8 * (XL_FR_MAX_EVENT_SIZE - XL_FR_RX_EVENT_HEADER_SIZE)),
    ]


class XLfrEvent(ctypes.Structure):  # noqa: N801
    _fields_ = [
        ('size', ctypes.c_uint32),
        ('tag', ctypes.c_uint16),
        ('channelIndex', ctypes.c_uint16),
        ('userHandle', ctypes.c_uint32),
        ('flagsChip', ctypes.c_uint16),
        ('reserved', ctypes.c_uint16),
        ('timeStamp', ctypes.c_uint64),  # Nanoseconds
        ('timeStampSync', ctypes.c_uint64),
        ('tagData', _TagData),
    ]


def load_library():
    if sys.platform != 'win32':
        raise FlexRayError('Vector interfaces are used through the XL Driver Library on Windows')
    name = 'vxlapi64' if ctypes.sizeof(ctypes.c_void_p) == 8 else 'vxlapi'
    try:
        return ctypes.WinDLL(name)
    except OSError as e:
        raise FlexRayError(
            f'Cannot load {name}.dll: install the Vector driver setup and the XL Driver Library ({e})'
        ) from e


def frame_from_event(event: XLfrEvent, offset: float) -> Optional[FlexRayFrame]:
    """A spy frame event as a frame (None for other events)."""
    if event.tag != XL_FR_SPY_FRAME:
        return None
    spy = event.tagData.frSpyFrame
    length = min(spy.payloadLength * 2, MAX_PAYLOAD)  # In 16-bit words (see the module documentation)
    channel = 'B' if event.flagsChip & XL_FR_CHANNEL_B and not event.flagsChip & XL_FR_CHANNEL_A else 'A'
    return FlexRayFrame(
        timestamp=offset + event.timeStamp / 1e9,
        channel=channel,
        slot=spy.slotID & 0x7FF,
        cycle=spy.cycleCount & 0x3F,
        payload=bytes(spy.data[:length]),
        null_frame=bool(spy.headerFlags & FLAG_NULLFRAME),
        sync=bool(spy.headerFlags & FLAG_SYNC),
        startup=bool(spy.headerFlags & FLAG_STARTUP),
        preamble=bool(spy.headerFlags & FLAG_PAYLOAD_PREAMBLE),
        errors=[name for bit, name in SPY_ERRORS.items() if spy.frameError & bit],
        header_crc=spy.headerCRC,
    )


class VectorFlexRaySpy:
    """The FlexRay channels of the Vector interfaces, and a spy on one of them. `library` replaces vxlapi (tests)."""

    def __init__(self, library: Any = None, application: str = 'CANExplorer'):
        self._lib = library if library is not None else load_library()
        self.application = application
        self._port = ctypes.c_long(-1)
        self._mask = 0
        self._offset = 0.0
        self.overflows = 0
        self._check(self._lib.xlOpenDriver(), 'xlOpenDriver')

    def _check(self, status: int, call: str) -> None:
        if status != XL_SUCCESS:
            text = ''
            try:
                text = self._lib.xlGetErrorString(status)
                text = text.decode() if isinstance(text, bytes) else str(text or '')
            except Exception:  # noqa: BLE001 - only for the message
                pass
            raise FlexRayError(f'{call} failed: {text or status}')

    def channels(self) -> List[dict]:
        """The channels with FlexRay: {'name', 'mask', 'serial'}."""
        from can.interfaces.vector.xlclass import XLdriverConfig

        config = XLdriverConfig()
        self._check(self._lib.xlGetDriverConfig(ctypes.byref(config)), 'xlGetDriverConfig')
        found = []
        for index in range(config.channelCount):
            channel = config.channel[index]
            if channel.channelBusCapabilities & XL_BUS_COMPATIBLE_FLEXRAY:
                name = bytes(channel.name).split(b'\0', 1)[0].decode(errors='replace')
                found.append({'name': name, 'mask': int(channel.channelMask), 'serial': int(channel.serialNumber)})
        return found

    def start(self, mask: int) -> None:
        """Open the channel (a mask from channels()) and start the spy."""
        access = ctypes.c_uint64(mask)
        permission = ctypes.c_uint64(0)
        self._check(
            self._lib.xlOpenPort(
                ctypes.byref(self._port),
                self.application.encode(),
                access,
                ctypes.byref(permission),
                8192,
                XL_INTERFACE_VERSION_V4,
                XL_BUS_TYPE_FLEXRAY,
            ),
            'xlOpenPort',
        )
        self._mask = mask
        self._check(self._lib.xlFrActivateSpy(self._port, access, XL_FR_SPY_MODE_ASYNCHRONOUS), 'xlFrActivateSpy')
        self._check(
            self._lib.xlActivateChannel(self._port, access, XL_BUS_TYPE_FLEXRAY, XL_ACTIVATE_RESET_CLOCK),
            'xlActivateChannel',
        )
        self._offset = time.time()  # The clock was reset on activation

    def receive(self, limit: int = 1000) -> List[FlexRayFrame]:
        """The frames received since the last call (none waiting: an empty list)."""
        frames = []
        event = XLfrEvent()
        for _ in range(limit):
            status = self._lib.xlFrReceive(self._port, ctypes.byref(event))
            if status == XL_ERR_QUEUE_IS_EMPTY:
                break
            self._check(status, 'xlFrReceive')
            if event.flagsChip & XL_FR_QUEUE_OVERFLOW:
                self.overflows += 1
            frame = frame_from_event(event, self._offset)
            if frame is not None:
                frames.append(frame)
        return frames

    def stop(self) -> None:
        try:
            if self._port.value != -1:
                access = ctypes.c_uint64(self._mask)
                self._lib.xlDeactivateChannel(self._port, access)
                self._lib.xlClosePort(self._port)
        finally:
            self._port = ctypes.c_long(-1)

    def close(self) -> None:
        self.stop()
        self._lib.xlCloseDriver()


def config_from_values(values: Dict[str, int]) -> XLfrClusterConfig:
    """An XLfrClusterConfig from field values named as in vxlapi.h. Raises FlexRayError for unknown names."""
    config = XLfrClusterConfig()
    unknown = sorted(set(values) - set(CLUSTER_CONFIG_FIELDS))
    if unknown:
        raise FlexRayError(f'Not XLfrClusterConfig fields: {", ".join(unknown)}')
    for name, value in values.items():
        try:
            setattr(config, name, int(value))
        except (TypeError, ValueError) as e:
            raise FlexRayError(f'XLfrClusterConfig.{name}: not a number ({value!r})') from e
    return config


def config_values(config: XLfrClusterConfig) -> Dict[str, int]:
    return {name: int(getattr(config, name)) for name in CLUSTER_CONFIG_FIELDS}


def load_config_file(path) -> Dict[str, int]:
    """XLfrClusterConfig field values from a JSON object ({"gMacroPerCycle": 5000, …}) or "name = value" lines."""
    import json
    from pathlib import Path

    text = Path(path).read_text(encoding='utf-8')
    try:
        values = json.loads(text)
    except json.JSONDecodeError:
        values = {}
        for line in text.splitlines():
            line = line.split('#', 1)[0].split('//', 1)[0].strip()
            if not line:
                continue
            name, _, value = line.replace(':', '=').partition('=')
            if not _:
                raise FlexRayError(f'Not "name = value": {line}') from None
            values[name.strip()] = value.strip().rstrip(',;')
    if not isinstance(values, dict):
        raise FlexRayError('The configuration is a JSON object of XLfrClusterConfig fields')
    return {name: int(str(value), 0) for name, value in values.items()}


class VectorFlexRayNode(VectorFlexRaySpy):
    """A FlexRay channel of a Vector interface as a node of the cluster: it receives the frames its controller
    receives and sends frames in the slots it is given (see the module documentation). EXPERIMENTAL."""

    def __init__(self, library: Any = None, application: str = 'CANExplorer'):
        super().__init__(library, application)
        self.events: List[FlexRayEvent] = []
        self.config: Optional[XLfrClusterConfig] = None
        self._transmitting: Dict[int, dict] = {}

    def channel_config(self, mask: int) -> Optional[XLfrClusterConfig]:
        """The configuration the channel has now (None when it has no valid cluster configuration)."""
        channel_config = XLfrChannelConfig()
        self._check(
            self._lib.xlFrGetChannelConfiguration(self._port, ctypes.c_uint64(mask), ctypes.byref(channel_config)),
            'xlFrGetChannelConfiguration',
        )
        if not channel_config.status & XL_FR_CHANNEL_CFG_STATUS_VALID_CLUSTER_CFG:
            return None
        return channel_config.xlFrClusterConfig

    def start(  # noqa: PLR0913 - the node's choices
        self,
        mask: int,
        config: Optional[Dict[str, int]] = None,
        coldstart: bool = False,
        key_slot: Optional[int] = None,
        key_payload: bytes = b'',
        key_channels: str = 'AB',
    ) -> None:
        """Join the cluster on the channel `mask`: with `config` (XLfrClusterConfig field values) or the channel's
        own configuration; as the leading coldstart node with the startup/sync frame of `key_slot` when
        `coldstart`."""
        access = ctypes.c_uint64(mask)
        permission = ctypes.c_uint64(mask)  # Init access is needed to configure the controller
        self._check(
            self._lib.xlOpenPort(
                ctypes.byref(self._port),
                self.application.encode(),
                access,
                ctypes.byref(permission),
                8192,
                XL_INTERFACE_VERSION_V4,
                XL_BUS_TYPE_FLEXRAY,
            ),
            'xlOpenPort',
        )
        self._mask = mask
        try:
            if config is not None:
                self.config = config_from_values(config)
            else:
                self.config = self.channel_config(mask)
                if self.config is None:
                    raise FlexRayError(
                        'The channel has no FlexRay cluster configuration: give one (XLfrClusterConfig values, as '
                        "Vector's tools export them), or configure the channel with a Vector application first"
                    )
            if permission.value & mask:
                self._check(
                    self._lib.xlFrSetConfiguration(self._port, access, ctypes.byref(self.config)),
                    'xlFrSetConfiguration',
                )
            elif config is not None:
                raise FlexRayError('Another application configures this channel (no init access)')
            mode = XLfrMode(
                frMode=XL_FR_MODE_NORMAL,
                frStartupAttributes=XL_FR_MODE_COLDSTART_LEADING if coldstart else XL_FR_MODE_NONE,
            )
            self._check(self._lib.xlFrSetMode(self._port, access, ctypes.byref(mode)), 'xlFrSetMode')
            if coldstart:
                if key_slot is None:
                    raise FlexRayError('A coldstart node needs its key slot (the startup and sync frame)')
                event = self._tx_event(key_slot, 0, 1, key_channels, key_payload, FLAG_STARTUP | FLAG_SYNC)
                self._check(
                    self._lib.xlFrInitStartupAndSync(self._port, access, ctypes.byref(event)),
                    'xlFrInitStartupAndSync',
                )
            self._check(
                self._lib.xlActivateChannel(self._port, access, XL_BUS_TYPE_FLEXRAY, XL_ACTIVATE_RESET_CLOCK),
                'xlActivateChannel',
            )
        except FlexRayError:
            self.stop()
            raise
        self._offset = time.time()

    @staticmethod
    def _tx_event(slot: int, base_cycle: int, repetition: int, channels: str, payload: bytes, flags: int = 0):
        if len(payload) % 2 or len(payload) > MAX_PAYLOAD:
            raise FlexRayError('A FlexRay payload has an even number of bytes, up to 254')
        event = XLfrEvent(tag=XL_FR_TX_FRAME, size=XL_FR_RX_EVENT_HEADER_SIZE + ctypes.sizeof(XL_FR_TX_FRAME_EV))
        event.flagsChip = (XL_FR_CHANNEL_A if 'A' in channels else 0) | (XL_FR_CHANNEL_B if 'B' in channels else 0)
        tx = event.tagData.frTxFrame
        tx.flags, tx.slotID, tx.offset, tx.repetition = flags, slot, base_cycle, repetition
        tx.payloadLength = len(payload) // 2  # In 16-bit words (see the module documentation)
        tx.txMode, tx.incrementSize = XL_FR_TX_MODE_CYCLIC, XL_FR_PAYLOAD_INCREMENT_NONE
        ctypes.memmove(tx.data, bytes(payload), len(payload))
        return event

    def transmit(self, slot: int, base_cycle: int, repetition: int, channels: str, payload: bytes) -> None:
        """Send `payload` in `slot` from `base_cycle`, every `repetition` cycles, until changed or stopped."""
        event = self._tx_event(slot, base_cycle, repetition, channels, payload)
        self._check(
            self._lib.xlFrTransmit(self._port, ctypes.c_uint64(self._mask), ctypes.byref(event)), 'xlFrTransmit'
        )
        self._transmitting[slot] = {'base': base_cycle, 'repetition': repetition, 'channels': channels}

    def stop_transmit(self, slot: int) -> None:
        entry = self._transmitting.pop(slot, None)
        if entry is None:
            return
        event = self._tx_event(slot, entry['base'], entry['repetition'], entry['channels'], b'')
        event.tagData.frTxFrame.txMode = XL_FR_TX_MODE_NONE
        self._check(
            self._lib.xlFrTransmit(self._port, ctypes.c_uint64(self._mask), ctypes.byref(event)), 'xlFrTransmit'
        )

    def receive(self, limit: int = 1000) -> List[FlexRayFrame]:
        frames = []
        event = XLfrEvent()
        for _ in range(limit):
            status = self._lib.xlFrReceive(self._port, ctypes.byref(event))
            if status == XL_ERR_QUEUE_IS_EMPTY:
                break
            self._check(status, 'xlFrReceive')
            if event.flagsChip & XL_FR_QUEUE_OVERFLOW:
                self.overflows += 1
            frame = node_frame_from_event(event, self._offset)
            if frame is not None:
                frames.append(frame)
                continue
            fr_event = node_event_from_event(event, self._offset)
            if fr_event is not None:
                self.events.append(fr_event)
        return frames

    def take_events(self) -> List[FlexRayEvent]:
        events, self.events = self.events, []
        return events


def _channels_of(flags_chip: int) -> str:
    return '+'.join(letter for bit, letter in ((XL_FR_CHANNEL_A, 'A'), (XL_FR_CHANNEL_B, 'B')) if flags_chip & bit)


def node_frame_from_event(event: XLfrEvent, offset: float) -> Optional[FlexRayFrame]:
    """A received (XL_FR_RX_FRAME, XL_FR_INVALID_FRAME) or sent (XL_FR_TXACK_FRAME) frame; a spy frame too.

    vxlapi.h does not say which member of the event union an XL_FR_TXACK_FRAME fills; it is read as the
    XL_FR_TX_FRAME_EV it acknowledges (an assumption), which has no cycle count: a sent frame shows its base cycle.
    """
    if event.tag == XL_FR_SPY_FRAME:
        return frame_from_event(event, offset)
    when = offset + event.timeStamp / 1e9
    channel = 'B' if event.flagsChip & XL_FR_CHANNEL_B and not event.flagsChip & XL_FR_CHANNEL_A else 'A'
    if event.tag == XL_FR_TXACK_FRAME:
        tx = event.tagData.frTxFrame
        length = min(tx.payloadLength * 2, MAX_PAYLOAD)
        return FlexRayFrame(
            timestamp=when,
            channel=channel,
            slot=tx.slotID & 0x7FF,
            cycle=tx.offset & 0x3F,
            payload=bytes(tx.data[:length]),
            null_frame=bool(tx.flags & FLAG_NULLFRAME),
            sync=bool(tx.flags & FLAG_SYNC),
            startup=bool(tx.flags & FLAG_STARTUP),
            transmitted=True,
        )
    if event.tag not in (XL_FR_RX_FRAME, XL_FR_INVALID_FRAME):
        return None
    rx = event.tagData.frRxFrame
    length = min(rx.payloadLength * 2, MAX_PAYLOAD)
    errors = [name for bit, name in FRAME_ERRORS.items() if rx.flags & bit]
    if event.tag == XL_FR_INVALID_FRAME and not errors:
        errors = ['invalid frame']
    return FlexRayFrame(
        timestamp=when,
        channel=channel,
        slot=rx.slotID & 0x7FF,
        cycle=rx.cycleCount & 0x3F,
        payload=bytes(rx.data[:length]),
        null_frame=bool(rx.flags & FLAG_NULLFRAME),
        sync=bool(rx.flags & FLAG_SYNC),
        startup=bool(rx.flags & FLAG_STARTUP),
        preamble=bool(rx.flags & FLAG_PAYLOAD_PREAMBLE),
        errors=errors,
        header_crc=rx.headerCRC,
    )


def node_event_from_event(event: XLfrEvent, offset: float) -> Optional[FlexRayEvent]:
    when = offset + event.timeStamp / 1e9
    channel = _channels_of(event.flagsChip)
    if event.tag == XL_FR_SYMBOL_WINDOW:
        window = event.tagData.frSymbolWindow
        name = {XL_FR_SYMBOL_MTS: 'MTS', XL_FR_SYMBOL_CAS: 'CAS'}.get(window.symbol, f'Symbol {window.symbol}')
        return FlexRayEvent(when, 'symbol', name, channel, window.cycleCount & 0x3F)
    if event.tag == XL_FR_STATUS:
        state = event.tagData.frStatus.statusType
        return FlexRayEvent(when, 'status', f'POC state: {POC_STATES.get(state, hex(state))}', channel)
    if event.tag == XL_FR_ERROR:
        error = event.tagData.frError
        text = ERROR_TAGS.get(error.tag, f'Error {error.tag}')
        return FlexRayEvent(when, 'error', text[:1].upper() + text[1:], channel, error.cycleCount & 0x3F)
    return None
