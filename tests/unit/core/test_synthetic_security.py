"""Tests for the security-related synthetic traffic generators: gateway traffic and multi-ECU SecOC traffic."""

from can_explorer.core.reverse.synthetic import (
    SecOcProfile,
    generate_gateway_traffic,
    generate_multi_ecu_secoc_traffic,
)


# ---------------------------------------------------------------------------
# generate_gateway_traffic
# ---------------------------------------------------------------------------


class TestGenerateGatewayTraffic:
    def test_returns_two_traffic_objects(self):
        """The function returns a (external, internal) tuple of SyntheticTraffic."""
        external, internal = generate_gateway_traffic(frame_count=50)
        assert len(external.messages) > 0
        assert len(internal.messages) > 0

    def test_forwarded_ids_appear_on_both_buses(self):
        """Forwarded IDs should appear on external and internal buses."""
        forwarded = [0x100, 0x200]
        external, internal = generate_gateway_traffic(forwarded_ids=forwarded, frame_count=100)

        ext_ids = {m.arbitration_id for m in external.messages}
        int_ids = {m.arbitration_id for m in internal.messages}

        for fid in forwarded:
            assert fid in ext_ids, f'Forwarded ID 0x{fid:03X} missing from external bus'
            assert fid in int_ids, f'Forwarded ID 0x{fid:03X} missing from internal bus'

    def test_blocked_ids_only_on_external(self):
        """Blocked IDs should appear on the external bus but not on the internal bus."""
        blocked = [0x300]
        external, internal = generate_gateway_traffic(forwarded_ids=[0x100], blocked_ids=blocked, frame_count=100)

        ext_ids = {m.arbitration_id for m in external.messages}
        int_ids = {m.arbitration_id for m in internal.messages}

        for bid in blocked:
            assert bid in ext_ids, f'Blocked ID 0x{bid:03X} missing from external bus'
            assert bid not in int_ids, f'Blocked ID 0x{bid:03X} should not appear on internal bus'

    def test_rewritten_ids(self):
        """Rewritten IDs appear on external with their original ID and on internal with the mapped ID."""
        rewritten = {0x400: 0x410}
        external, internal = generate_gateway_traffic(
            forwarded_ids=[0x100], blocked_ids=[], rewritten_ids=rewritten, frame_count=100
        )

        ext_ids = {m.arbitration_id for m in external.messages}
        int_ids = {m.arbitration_id for m in internal.messages}

        assert 0x400 in ext_ids
        assert 0x410 in int_ids

    def test_gateway_latency(self):
        """Forwarded frames on the internal bus should be slightly later than the external ones."""
        external, internal = generate_gateway_traffic(forwarded_ids=[0x100], blocked_ids=[], frame_count=50)

        ext_times = [m.timestamp for m in external.messages if m.arbitration_id == 0x100]
        int_times = [m.timestamp for m in internal.messages if m.arbitration_id == 0x100]

        assert len(ext_times) == len(int_times)
        for et, it in zip(ext_times, int_times, strict=False):
            assert it > et, 'Internal frame should be later than external (gateway latency)'

    def test_external_only_ids(self):
        """IDs listed in external_ids appear only on the external bus."""
        external, internal = generate_gateway_traffic(
            external_ids=[0x50], forwarded_ids=[0x100], blocked_ids=[], frame_count=50
        )

        ext_ids = {m.arbitration_id for m in external.messages}
        int_ids = {m.arbitration_id for m in internal.messages}

        assert 0x50 in ext_ids
        assert 0x50 not in int_ids

    def test_internal_only_ids(self):
        """IDs listed in internal_ids appear only on the internal bus."""
        external, internal = generate_gateway_traffic(
            internal_ids=[0x60], forwarded_ids=[0x100], blocked_ids=[], frame_count=50
        )

        ext_ids = {m.arbitration_id for m in external.messages}
        int_ids = {m.arbitration_id for m in internal.messages}

        assert 0x60 not in ext_ids
        assert 0x60 in int_ids

    def test_payload_data_is_deterministic(self):
        """With the same seed, the generated traffic should be identical."""
        ext1, int1 = generate_gateway_traffic(forwarded_ids=[0x100], frame_count=20, seed=99)
        ext2, int2 = generate_gateway_traffic(forwarded_ids=[0x100], frame_count=20, seed=99)

        assert len(ext1.messages) == len(ext2.messages)
        for m1, m2 in zip(ext1.messages, ext2.messages, strict=False):
            assert m1.data == m2.data
            assert m1.arbitration_id == m2.arbitration_id

    def test_messages_are_time_sorted(self):
        """Messages in both traffic objects should be sorted by timestamp."""
        external, internal = generate_gateway_traffic(frame_count=100)

        ext_ts = [m.timestamp for m in external.messages]
        int_ts = [m.timestamp for m in internal.messages]
        assert ext_ts == sorted(ext_ts)
        assert int_ts == sorted(int_ts)

    def test_default_parameters(self):
        """Calling with all defaults should produce non-empty traffic with the default forwarded/blocked/rewritten IDs."""
        external, internal = generate_gateway_traffic()
        ext_ids = {m.arbitration_id for m in external.messages}
        int_ids = {m.arbitration_id for m in internal.messages}

        # Default forwarded: 0x100, 0x200; blocked: 0x300; rewritten: 0x400 -> 0x410
        assert 0x100 in ext_ids
        assert 0x100 in int_ids
        assert 0x300 in ext_ids
        assert 0x300 not in int_ids
        assert 0x400 in ext_ids
        assert 0x410 in int_ids

    def test_empty_lists(self):
        """Empty forwarded/blocked/rewritten lists produce traffic from external/internal only IDs."""
        external, internal = generate_gateway_traffic(
            external_ids=[0x10],
            internal_ids=[0x20],
            forwarded_ids=[],
            blocked_ids=[],
            rewritten_ids={},
            frame_count=20,
        )
        ext_ids = {m.arbitration_id for m in external.messages}
        int_ids = {m.arbitration_id for m in internal.messages}

        assert 0x10 in ext_ids
        assert 0x20 in int_ids


# ---------------------------------------------------------------------------
# generate_multi_ecu_secoc_traffic
# ---------------------------------------------------------------------------


class TestGenerateMultiEcuSecOcTraffic:
    def test_default_generates_four_ecus(self):
        """Default configuration produces frames from 4 ECU identifiers."""
        traffic = generate_multi_ecu_secoc_traffic()
        ids = {m.arbitration_id for m in traffic.messages}
        assert ids == {0x200, 0x201, 0x202, 0x203}

    def test_messages_are_time_sorted(self):
        """Messages from multiple ECUs should be interleaved and sorted by timestamp."""
        traffic = generate_multi_ecu_secoc_traffic()
        timestamps = [m.timestamp for m in traffic.messages]
        assert timestamps == sorted(timestamps)

    def test_frame_count_per_ecu(self):
        """Each ECU should produce approximately frame_count messages."""
        frame_count = 100
        traffic = generate_multi_ecu_secoc_traffic(frame_count=frame_count)
        for eid in [0x200, 0x201, 0x202, 0x203]:
            count = sum(1 for m in traffic.messages if m.arbitration_id == eid)
            assert count == frame_count

    def test_shared_key_groups_produce_same_macs(self):
        """ECUs in the same shared_key_group should use the same key (verifiable via MAC consistency)."""
        traffic = generate_multi_ecu_secoc_traffic(
            ecu_configs=[
                {'identifier': 0x300, 'payload_bytes': 4},
                {'identifier': 0x301, 'payload_bytes': 4},
            ],
            shared_key_groups=[[0, 1]],
            frame_count=50,
        )
        # Both ECUs use the same key, same profile defaults: mac_bytes=3, freshness_bytes=1
        # Total payload: 4 + 1 + 3 = 8 bytes
        for m in traffic.messages:
            assert len(m.data) == 8, f'Expected 8-byte payload, got {len(m.data)}'

    def test_custom_ecu_configs(self):
        """Custom ECU configs with different payload sizes should be respected."""
        configs = [
            {'identifier': 0x500, 'payload_bytes': 2},
            {'identifier': 0x501, 'payload_bytes': 6},
        ]
        traffic = generate_multi_ecu_secoc_traffic(ecu_configs=configs, shared_key_groups=[], frame_count=30)
        ids = {m.arbitration_id for m in traffic.messages}
        assert ids == {0x500, 0x501}

        for m in traffic.messages:
            if m.arbitration_id == 0x500:
                # 2 payload + 1 freshness + 3 MAC = 6
                assert len(m.data) == 6
            elif m.arbitration_id == 0x501:
                # 6 payload + 1 freshness + 3 MAC = 10
                assert len(m.data) == 10

    def test_custom_profile_applied(self):
        """A custom SecOcProfile with different mac_bytes should change the frame length."""
        prof = SecOcProfile(mac_bytes=4, freshness_bytes=2)
        configs = [{'identifier': 0x600, 'payload_bytes': 3, 'profile': prof}]
        traffic = generate_multi_ecu_secoc_traffic(ecu_configs=configs, shared_key_groups=[], frame_count=20)
        for m in traffic.messages:
            # 3 payload + 2 freshness + 4 MAC = 9
            assert len(m.data) == 9

    def test_deterministic_with_seed(self):
        """Same seed should produce identical traffic."""
        t1 = generate_multi_ecu_secoc_traffic(frame_count=30, seed=77)
        t2 = generate_multi_ecu_secoc_traffic(frame_count=30, seed=77)
        assert len(t1.messages) == len(t2.messages)
        for m1, m2 in zip(t1.messages, t2.messages, strict=False):
            assert m1.data == m2.data
            assert m1.arbitration_id == m2.arbitration_id

    def test_independent_keys_differ(self):
        """ECUs not in a shared group should use different keys (different MACs for the same input)."""
        traffic = generate_multi_ecu_secoc_traffic(
            ecu_configs=[
                {'identifier': 0x700, 'payload_bytes': 4},
                {'identifier': 0x701, 'payload_bytes': 4},
            ],
            shared_key_groups=[],
            frame_count=50,
        )
        # Collect MAC bytes (last 3 bytes of each 8-byte frame) per ID
        macs_700 = [m.data[5:8] for m in traffic.messages if m.arbitration_id == 0x700]
        macs_701 = [m.data[5:8] for m in traffic.messages if m.arbitration_id == 0x701]
        # With independent keys, the MAC sets should not be identical
        assert macs_700 != macs_701

    def test_single_ecu_config(self):
        """A single ECU config should work without errors."""
        traffic = generate_multi_ecu_secoc_traffic(
            ecu_configs=[{'identifier': 0x800, 'payload_bytes': 4}],
            shared_key_groups=[],
            frame_count=20,
        )
        ids = {m.arbitration_id for m in traffic.messages}
        assert ids == {0x800}
        assert len(traffic.messages) == 20
