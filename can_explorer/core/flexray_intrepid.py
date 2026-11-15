"""FlexRay through an Intrepid Control Systems interface (neoVI FIRE 3 FlexRay, RAD-Galaxy, …) with Intrepid's
open-source libicsneo Python module, icsneopy (`pip install icsneopy`; its licence allows it with Intrepid
hardware only).

EXPERIMENTAL: written from icsneopy's published bindings (bindings/python/icsneopy/flexray/flexray.cpp and
device/device.cpp) and FlexRay examples, and tested against a simulated module, not yet on a device. Used:
find_all_devices, Device.get_extension("FlexRay"), get_flexray_controllers, open, go_online, enable_message_polling,
get_messages, transmit, go_offline, close; Controller.get_network, set_configuration (Cluster.Configuration and
Controller.Configuration), set_allow_coldstart, set_start_when_going_online; FlexRayMessage (slotid, cycle,
cycle_repetition, channel, data, null_frame, sync_frame, startup_frame, payload_preamble, header_crc,
header_crc_status, frame_crc_status, symbol, transmitted, error, timestamp).

A controller listens with the configuration the device already has (set up with Intrepid's tools), or joins the
cluster with one made from the cluster description (node_configuration): the cluster's parameters (AUTOSAR
FlexrayCluster) and an ECU's FlexRay controller (FlexrayCommunicationController), which icsneopy takes in the same
units (macroticks, microticks, bits; the cycle and macrotick in µs). Timestamps are taken in nanoseconds, as
libicsneo gives them, and placed on this computer's clock at the first message.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from can_explorer.core.flexray import MAX_PAYLOAD, FlexRayError, FlexRayEvent, FlexRayFrame

# icsneopy Cluster.Configuration field ← AUTOSAR FlexrayCluster parameter (ARXML name), scale
CLUSTER_FIELDS = {
    'action_point_offset': ('ACTION-POINT-OFFSET', 1),
    'casr_x_low_max': ('CAS-RX-LOW-MAX', 1),
    'cold_start_attempts': ('COLD-START-ATTEMPTS', 1),
    'cycle_duration_micro_sec': ('CYCLE', 1e6),
    'dynamic_slot_idle_phase_minislots': ('DYNAMIC-SLOT-IDLE-PHASE', 1),
    'listen_noise_macroticks': ('LISTEN-NOISE', 1),
    'macroticks_per_cycle': ('MACRO-PER-CYCLE', 1),
    'macrotick_duration_micro_sec': ('MACROTICK-DURATION', 1e6),
    'max_without_clock_correction_fatal': ('MAX-WITHOUT-CLOCK-CORRECTION-FATAL', 1),
    'max_without_clock_correction_passive': ('MAX-WITHOUT-CLOCK-CORRECTION-PASSIVE', 1),
    'minislot_action_point_offset_macroticks': ('MINISLOT-ACTION-POINT-OFFSET', 1),
    'minislot_duration_macroticks': ('MINISLOT-DURATION', 1),
    'network_idle_time_macroticks': ('NETWORK-IDLE-TIME', 1),
    'network_management_vector_length_bytes': ('NETWORK-MANAGEMENT-VECTOR-LENGTH', 1),
    'number_of_minislots': ('NUMBER-OF-MINISLOTS', 1),
    'number_of_static_slots': ('NUMBER-OF-STATIC-SLOTS', 1),
    'offset_correction_start_macroticks': ('OFFSET-CORRECTION-START', 1),
    'payload_length_of_static_slot_in_words': ('PAYLOAD-LENGTH-STATIC', 1),
    'static_slot_macroticks': ('STATIC-SLOT-DURATION', 1),
    'symbol_window_macroticks': ('SYMBOL-WINDOW', 1),
    'symbol_window_action_point_offset_macroticks': ('SYMBOL-WINDOW-ACTION-POINT-OFFSET', 1),
    'sync_frame_id_count_max': ('SYNC-FRAME-ID-COUNT-MAX', 1),
    'transmission_start_sequence_duration_bits': ('TRANSMISSION-START-SEQUENCE-DURATION', 1),
    'wakeup_rx_idle_bits': ('WAKEUP-RX-IDLE', 1),
    'wakeup_rx_low_bits': ('WAKEUP-RX-LOW', 1),
    'wakeup_rx_window_bits': ('WAKEUP-RX-WINDOW', 1),
    'wakeup_tx_active_bits': ('WAKEUP-TX-ACTIVE', 1),
    'wakeup_tx_idle_bits': ('WAKEUP-TX-IDLE', 1),
}
# icsneopy Controller.Configuration field ← AUTOSAR FlexrayCommunicationController parameter
CONTROLLER_FIELDS = {
    'accept_startup_range_microticks': 'ACCEPTED-STARTUP-RANGE',
    'allow_halt_due_to_clock': 'ALLOW-HALT-DUE-TO-CLOCK',
    'allow_passive_to_active_cycle_pairs': 'ALLOW-PASSIVE-TO-ACTIVE',
    'cluster_drift_damping': 'CLUSTER-DRIFT-DAMPING',
    'decoding_correction_microticks': 'DECODING-CORRECTION',
    'delay_compensation_a_microticks': 'DELAY-COMPENSATION-A',
    'delay_compensation_b_microticks': 'DELAY-COMPENSATION-B',
    'extern_offset_correction_microticks': 'EXTERN-OFFSET-CORRECTION',
    'extern_rate_correction_microticks': 'EXTERN-RATE-CORRECTION',
    'key_slot_id': 'KEY-SLOT-ID',
    'key_slot_only_enabled': 'KEY-SLOT-ONLY-ENABLED',
    'key_slot_used_for_startup': 'KEY-SLOT-USED-FOR-START-UP',
    'key_slot_used_for_sync': 'KEY-SLOT-USED-FOR-SYNC',
    'latest_tx_minislot': 'LATEST-TX',
    'listen_timeout': 'LISTEN-TIMEOUT',
    'macro_initial_offset_a': 'MACRO-INITIAL-OFFSET-A',
    'macro_initial_offset_b': 'MACRO-INITIAL-OFFSET-B',
    'micro_initial_offset_a': 'MICRO-INITIAL-OFFSET-A',
    'micro_initial_offset_b': 'MICRO-INITIAL-OFFSET-B',
    'micro_per_cycle': 'MICRO-PER-CYCLE',
    'offset_correction_out_microticks': 'OFFSET-CORRECTION-OUT',
    'rate_correction_out_microticks': 'RATE-CORRECTION-OUT',
    'second_key_slot_id': 'SECOND-KEY-SLOT-ID',
    'two_key_slot_mode': 'TWO-KEY-SLOT-MODE',
    'wakeup_pattern': 'WAKE-UP-PATTERN',
}
OPTIONAL = {
    'symbol_window_macroticks',
    'symbol_window_action_point_offset_macroticks',
    'extern_offset_correction_microticks',
    'extern_rate_correction_microticks',
    'key_slot_only_enabled',
    'second_key_slot_id',
    'two_key_slot_mode',
    'delay_compensation_a_microticks',
    'delay_compensation_b_microticks',
    'macro_initial_offset_a',
    'macro_initial_offset_b',
    'micro_initial_offset_a',
    'micro_initial_offset_b',
    'key_slot_id',
    'key_slot_used_for_startup',
    'key_slot_used_for_sync',
}
SPEEDS = {10_000_000: 'FLEXRAY_BAUDRATE_10M', 5_000_000: 'FLEXRAY_BAUDRATE_5M', 2_500_000: 'FLEXRAY_BAUDRATE_2M5'}


def _value(text: str) -> Any:
    if text.lower() in ('true', 'false'):
        return text.lower() == 'true'
    number = float(text)
    return int(number) if number == int(number) else number


def node_configuration(database: Any, ecu: str) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """icsneopy's cluster and controller configuration fields for joining `database`'s cluster as `ecu`, and the
    parameters the description does not give (which the controller needs). ARXML descriptions only: FIBEX
    controllers are not read."""
    if ecu not in database.controllers:
        raise FlexRayError(f'The cluster description has no FlexRay controller of {ecu!r}')
    parameters = database.parameters.values
    cluster: Dict[str, Any] = {}
    missing: List[str] = []
    for name, (source, scale) in CLUSTER_FIELDS.items():
        if source in parameters:
            value = _value(parameters[source])
            cluster[name] = int(round(value * scale)) if scale != 1 else value
        elif name not in OPTIONAL:
            missing.append(source)
    if database.parameters.baudrate:
        if database.parameters.baudrate not in SPEEDS:
            raise FlexRayError(f'icsneopy has no FlexRay bit rate {database.parameters.baudrate} bit/s')
        cluster['speed'] = SPEEDS[database.parameters.baudrate]
    else:
        missing.append('BAUDRATE')
    values = database.controllers[ecu]
    controller: Dict[str, Any] = {}
    for name, source in CONTROLLER_FIELDS.items():
        if source in values:
            controller[name] = _value(values[source])
        elif name not in OPTIONAL:
            missing.append(source)
    channels = set().union(*(d.channels for d in database.definitions)) if database.definitions else {'A'}
    controller['channel_a'], controller['channel_b'] = 'A' in channels, 'B' in channels
    return cluster, controller, missing


def load_module():
    try:
        import icsneopy
    except ImportError as e:
        raise FlexRayError(
            "Intrepid interfaces need Intrepid's icsneopy module: pip install icsneopy (or uv pip install icsneopy)"
        ) from e
    return icsneopy


class IntrepidFlexRay:
    """The FlexRay controllers of the Intrepid devices, and one of them listening or taking part in the cluster.
    `module` replaces icsneopy (tests)."""

    def __init__(self, module: Any = None):
        self._ics = module if module is not None else load_module()
        self._devices: List[Any] = []
        self._device = None
        self._controller = None
        self._offset: Optional[float] = None
        self.events: List[FlexRayEvent] = []

    def channels(self) -> List[dict]:
        """One entry per FlexRay controller: {'name', 'device', 'controller'}."""
        self._devices = [device for device in self._ics.find_all_devices() if device.get_extension('FlexRay')]
        found = []
        for index, device in enumerate(self._devices):
            name = device.describe() if hasattr(device, 'describe') else str(device)
            for number, _ in enumerate(device.get_flexray_controllers()):
                found.append({'name': f'{name} FlexRay {number + 1}', 'device': index, 'controller': number})
        return found

    def start(
        self,
        device: int,
        controller: int,
        configuration: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None,
        coldstart: bool = False,
    ) -> None:
        """Go online with a controller: with the device's own configuration, or `configuration` (the cluster and
        controller fields of node_configuration); allowed to coldstart the cluster when `coldstart`."""
        if not self._devices:
            self.channels()
        if not 0 <= device < len(self._devices):
            raise FlexRayError('No such Intrepid device (find the channels first)')
        self._device = self._devices[device]
        controllers = self._device.get_flexray_controllers()
        if not 0 <= controller < len(controllers):
            raise FlexRayError('The device has no such FlexRay controller')
        self._controller = controllers[controller]
        if configuration is not None:
            cluster_values, controller_values = configuration
            cluster = self._ics.FlexRay.Cluster.Configuration()
            for name, value in cluster_values.items():
                if name == 'speed':
                    value = getattr(self._ics.FlexRay.Cluster.SpeedType, value)
                setattr(cluster, name, value)
            node = self._ics.FlexRay.Controller.Configuration()
            for name, value in controller_values.items():
                setattr(node, name, value)
            self._controller.set_allow_coldstart(bool(coldstart))
            self._controller.set_configuration(cluster, node)
            self._controller.set_start_when_going_online(True)
        if not self._device.open():
            raise FlexRayError('Cannot open the Intrepid device')
        if not self._device.go_online():
            self._device.close()
            raise FlexRayError('The Intrepid device does not go online')
        self._device.enable_message_polling()
        self._offset = None

    def _timestamp(self, nanoseconds: int) -> float:
        if self._offset is None:
            self._offset = time.time() - nanoseconds / 1e9
        return self._offset + nanoseconds / 1e9

    def receive(self, limit: int = 1000) -> List[FlexRayFrame]:
        if self._device is None:
            return []
        messages = self._device.get_messages()
        if isinstance(messages, tuple):  # (messages, success)
            messages = messages[0]
        frames = []
        FlexRay = self._ics.FlexRay  # noqa: N806 - the module's name
        for message in list(messages)[:limit] if limit else messages:
            if not isinstance(message, self._ics.FlexRayMessage):
                continue
            when = self._timestamp(int(message.timestamp))
            symbol = str(message.symbol).rsplit('.', 1)[-1]  # FlexRay.Symbol: None, Unknown, Wakeup, CAS
            if symbol in ('Unknown', 'Wakeup', 'CAS'):
                text = {'Wakeup': 'Wakeup symbol', 'Unknown': 'Symbol'}.get(symbol, symbol)
                self.events.append(FlexRayEvent(when, 'symbol', text, _channel_name(message.channel)))
                continue
            errors = []
            if message.header_crc_status == FlexRay.CRCStatus.Error:
                errors.append('header CRC')
            if message.frame_crc_status == FlexRay.CRCStatus.Error:
                errors.append('frame CRC')
            if getattr(message, 'error', False) and not errors:
                errors.append('invalid frame')
            payload = bytes(message.data)[:MAX_PAYLOAD]
            for channel in _channel_name(message.channel).split('+'):
                if channel not in ('A', 'B'):
                    continue
                frames.append(
                    FlexRayFrame(
                        timestamp=when,
                        channel=channel,
                        slot=int(message.slotid) & 0x7FF,
                        cycle=int(message.cycle) & 0x3F,
                        payload=payload,
                        null_frame=bool(message.null_frame),
                        sync=bool(message.sync_frame),
                        startup=bool(message.startup_frame),
                        preamble=bool(message.payload_preamble),
                        errors=list(errors),
                        header_crc=int(message.header_crc),
                        transmitted=bool(getattr(message, 'transmitted', False)),
                    )
                )
        return frames

    def take_events(self) -> List[FlexRayEvent]:
        events, self.events = self.events, []
        return events

    def transmit(self, slot: int, base_cycle: int, repetition: int, channels: str, payload: bytes) -> None:
        if self._device is None or self._controller is None:
            raise FlexRayError('Go online first')
        if len(payload) % 2 or len(payload) > MAX_PAYLOAD:
            raise FlexRayError('A FlexRay payload has an even number of bytes, up to 254')
        message = self._ics.FlexRayMessage()
        message.network = self._controller.get_network()
        message.slotid, message.cycle, message.cycle_repetition = slot, base_cycle, repetition
        both = 'A' in channels and 'B' in channels
        message.channel = getattr(self._ics.FlexRay.Channel, 'AB' if both else ('B' if 'B' in channels else 'A'))
        message.data = tuple(payload)
        if not self._device.transmit(message):
            raise FlexRayError(f'The Intrepid device did not take the frame for slot {slot}')

    def stop(self) -> None:
        if self._device is not None:
            try:
                self._device.go_offline()
            finally:
                self._device.close()
                self._device = None

    def close(self) -> None:
        self.stop()


def _channel_name(channel: Any) -> str:
    name = str(channel).rsplit('.', 1)[-1]
    return {'A': 'A', 'B': 'B', 'AB': 'A+B'}.get(name, '')
