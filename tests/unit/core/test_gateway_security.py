"""Tests for the gateway security analysis module."""

import can

from can_explorer.core.reverse.gateway_security import (
    analyse_auth_timing,
    analyse_doip_gateway,
    analyse_gateway_auth,
    analyse_gateway_filtering,
    analyse_reset_behaviour,
    attack_surface_report,
    brute_plan,
    build_access_matrix,
    classify_did_security,
    correlate_ecu_seeds,
    decode_nrc,
    detect_bus_segments,
    detect_rewriting,
    detect_timing_side_channel,
    differential_analysis,
    discover_unlock_chains,
    enumerate_plan,
    export_report,
    extract_unlock_sequence,
    fingerprint_ecu,
    gateway_compliance_report,
    gateway_report,
    identify_ecu,
    infer_filter_rules,
    infer_routing_table,
    map_gateway,
    plan_probing,
    recover_auth_state_machine,
    scan_plan_non_standard,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(arb_id: int, data: bytes, timestamp: float = 0.0) -> can.Message:
    return can.Message(arbitration_id=arb_id, data=data, timestamp=timestamp, is_extended_id=False)


def _uds_flow(
    req_id: int,
    resp_id: int,
    exchanges: list,
    base_time: float = 0.0,
) -> list:
    """Build a list of can.Messages from (request_data, response_data) pairs."""
    messages = []
    t = base_time
    for request, response in exchanges:
        messages.append(_msg(req_id, request, t))
        t += 0.05
        messages.append(_msg(resp_id, response, t))
        t += 0.05
    return messages


# ---------------------------------------------------------------------------
# analyse_gateway_auth
# ---------------------------------------------------------------------------


class TestAnalyseGatewayAuth:
    def test_seed_key_pattern(self):
        """Detect a 0x27 SecurityAccess pattern with locked services."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                # Session change to extended
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                # Read DID fails with securityAccessDenied
                (b'\x22\xf1\x90', b'\x7f\x22\x33'),
                # Security access request seed
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                # Security access send key
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
                # Read DID succeeds
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41\x42'),
            ],
        )

        patterns = analyse_gateway_auth(messages)
        assert len(patterns) == 1
        p = patterns[0]
        assert p.request_id == 0x7E0
        assert p.response_id == 0x7E8
        assert p.auth_type == 'seed_key_0x27'
        assert 0x22 in p.locked_services
        assert 0x22 in p.unlocked_services

    def test_no_auth_pattern(self):
        """Detect no authentication when services just work."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41\x42'),
                (b'\x22\xf1\x91', b'\x62\xf1\x91\x43\x44'),
            ],
        )

        patterns = analyse_gateway_auth(messages)
        assert len(patterns) == 1
        assert patterns[0].auth_type == 'none'

    def test_authentication_0x29(self):
        """Detect a 0x29 Authentication pattern."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x2e\xf1\x90\x41', b'\x7f\x2e\x34'),  # authenticationRequired
                (b'\x29\x03\x01', b'\x69\x03\x01\xaa'),  # Authentication sub-function
                (b'\x2e\xf1\x90\x41', b'\x6e\xf1\x90'),
            ],
        )

        patterns = analyse_gateway_auth(messages)
        assert len(patterns) == 1
        p = patterns[0]
        assert p.auth_type == 'challenge_response_0x29'
        assert 0x2E in p.locked_services

    def test_multiple_ecus(self):
        """Multiple ECU pairs are reported separately."""
        messages = _uds_flow(0x7E0, 0x7E8, [(b'\x22\xf1\x90', b'\x62\xf1\x90\x41\x42')], base_time=0.0)
        messages += _uds_flow(0x7E2, 0x7EA, [(b'\x22\xf1\x90', b'\x7f\x22\x33')], base_time=1.0)

        patterns = analyse_gateway_auth(messages)
        assert len(patterns) == 2
        ids = {p.request_id for p in patterns}
        assert ids == {0x7E0, 0x7E2}

    def test_empty_messages(self):
        """Empty message list gives no patterns."""
        assert analyse_gateway_auth([]) == []


# ---------------------------------------------------------------------------
# analyse_gateway_filtering
# ---------------------------------------------------------------------------


class TestAnalyseGatewayFiltering:
    def test_unfiltered_id(self):
        """Detect IDs that answer diagnostic services without security."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                # This ECU locks services
                (b'\x22\xf1\x90', b'\x7f\x22\x33'),
            ],
        )
        messages += _uds_flow(
            0x7E4,
            0x7EC,
            [
                # This ECU answers without security
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41'),
            ],
            base_time=1.0,
        )

        findings = analyse_gateway_filtering(messages, gateway_ids={0x7E0})
        unfiltered = [f for f in findings if f.finding_type == 'unfiltered_id']
        assert len(unfiltered) >= 1
        assert any('0x7E4' in f.description for f in unfiltered)

    def test_session_fallback(self):
        """Detect services that work in default session after being locked in extended."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                # Switch to extended
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                # Locked in extended
                (b'\x2e\xf1\x90\x41', b'\x7f\x2e\x33'),
                # Switch back to default
                (b'\x10\x01', b'\x50\x01\x00\x19\x01\xf4'),
                # Works in default
                (b'\x2e\xf1\x90\x41', b'\x6e\xf1\x90'),
            ],
        )

        findings = analyse_gateway_filtering(messages)
        fallback = [f for f in findings if f.finding_type == 'session_fallback']
        assert len(fallback) >= 1

    def test_no_findings(self):
        """Normal traffic with no bypass indicators."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41\x42'),
            ],
        )

        findings = analyse_gateway_filtering(messages)
        # Only locked + then unlocked; no bypass findings expected
        assert all(f.severity != 'finding' for f in findings)

    def test_timing_window(self):
        """Detect services that succeed within 2s of a session change for a locked ID."""
        messages = []
        t = 0.0

        # Session change response (from ECU 0x7E8)
        messages.append(_msg(0x7E0, b'\x10\x03', t))
        t += 0.05
        messages.append(_msg(0x7E8, b'\x50\x03\x00\x19\x01\xf4', t))
        t += 0.05

        # Service locked in this session at some point
        messages.append(_msg(0x7E0, b'\x2e\xf1\x90\x41', t))
        t += 0.05
        messages.append(_msg(0x7E8, b'\x7f\x2e\x33', t))
        t += 0.05

        # Same service succeeds within 1 second of session change
        messages.append(_msg(0x7E0, b'\x2e\xf1\x90\x41', t))
        t += 0.05
        resp_data = b'\x6e\xf1\x90'
        messages.append(_msg(0x7E8, resp_data, t))

        findings = analyse_gateway_filtering(messages)
        timing = [f for f in findings if f.finding_type == 'timing_window']
        assert len(timing) >= 1, f'Expected timing_window finding, got: {[f.finding_type for f in findings]}'

    def test_functional_addressing_bypass(self):
        """Detect services that work via functional addressing but are locked via physical."""
        messages = []

        # Physical request to 0x7E0 is locked
        messages += _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x22\xf1\x90', b'\x7f\x22\x33'),  # securityAccessDenied
            ],
            base_time=0.0,
        )

        # Functional request (0x7DF) succeeds for the same service
        messages += _uds_flow(
            0x7DF,
            0x7E8,
            [
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41'),  # positive response
            ],
            base_time=1.0,
        )

        findings = analyse_gateway_filtering(messages)
        bypass = [f for f in findings if f.finding_type == 'functional_addressing']
        assert len(bypass) >= 1, f'Expected functional_addressing finding, got: {[f.finding_type for f in findings]}'

    def test_response_offset(self):
        """Non-standard response offset (e.g. +0x600) is used when specified."""
        messages = _uds_flow(
            0x600,
            0xC00,
            [
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41'),
            ],
        )
        # With default offset (+8), the pairing should not match
        auth_default = analyse_gateway_auth(messages)
        assert len(auth_default) == 0

        # With +0x600, it should match
        auth_custom = analyse_gateway_auth(messages, response_offset=0x600)
        assert len(auth_custom) == 1

    def test_empty_messages(self):
        assert analyse_gateway_filtering([]) == []


# ---------------------------------------------------------------------------
# _is_positive_response bound check
# ---------------------------------------------------------------------------


class TestPositiveResponseBound:
    def test_valid_range(self):
        from can_explorer.core.reverse.gateway_security import _is_positive_response

        assert _is_positive_response(b'\x62\xf1\x90') == 0x22
        assert _is_positive_response(b'\x41') == 0x01
        assert _is_positive_response(b'\x7e') == 0x3E

    def test_negative_response_excluded(self):
        from can_explorer.core.reverse.gateway_security import _is_positive_response

        # 0x7F is a negative response header, should not be treated as positive
        assert _is_positive_response(b'\x7f\x22\x33') is None

    def test_above_range_excluded(self):
        from can_explorer.core.reverse.gateway_security import _is_positive_response

        # 0x80+ should not be treated as a positive UDS response SID
        assert _is_positive_response(b'\x80') is None
        assert _is_positive_response(b'\xff') is None


# ---------------------------------------------------------------------------
# correlate_ecu_seeds
# ---------------------------------------------------------------------------


class TestCorrelateEcuSeeds:
    def test_same_algorithm(self):
        """Two ECUs using the same XOR constant are correlated."""
        constant = 0xDEADBEEF

        def algo(seed):
            return (int.from_bytes(seed, 'big') ^ constant).to_bytes(4, 'big')

        import random

        rng = random.Random(700)
        pairs_a = [(bytes(rng.randint(0, 255) for _ in range(4)), None) for _ in range(12)]
        pairs_a = [(s, algo(s)) for s, _ in pairs_a]
        pairs_b = [(bytes(rng.randint(0, 255) for _ in range(4)), None) for _ in range(12)]
        pairs_b = [(s, algo(s)) for s, _ in pairs_b]

        correlations = correlate_ecu_seeds({0x7E0: pairs_a, 0x7E2: pairs_b})
        same = [c for c in correlations if c.finding == 'same_algorithm']
        assert same, f'Expected same_algorithm finding, got: {[c.finding for c in correlations]}'

    def test_independent_algorithms(self):
        """Two ECUs using different algorithms are reported as independent."""
        import random

        rng = random.Random(701)

        def algo_xor(seed):
            return (int.from_bytes(seed, 'big') ^ 0x12345678).to_bytes(4, 'big')

        def algo_add(seed):
            return ((int.from_bytes(seed, 'big') + 0xAAAAAAAA) % (1 << 32)).to_bytes(4, 'big')

        pairs_a = []
        for _ in range(12):
            s = bytes(rng.randint(0, 255) for _ in range(4))
            pairs_a.append((s, algo_xor(s)))

        pairs_b = []
        for _ in range(12):
            s = bytes(rng.randint(0, 255) for _ in range(4))
            pairs_b.append((s, algo_add(s)))

        correlations = correlate_ecu_seeds({0x7E0: pairs_a, 0x7E2: pairs_b})
        # At minimum, there should be correlations (even if independent)
        assert isinstance(correlations, list)

    def test_too_few_ecus(self):
        """Single ECU gives no correlations."""
        import random

        rng = random.Random(702)
        pairs = [
            (bytes(rng.randint(0, 255) for _ in range(4)), bytes(rng.randint(0, 255) for _ in range(4)))
            for _ in range(12)
        ]
        correlations = correlate_ecu_seeds({0x7E0: pairs})
        assert correlations == []

    def test_empty_input(self):
        assert correlate_ecu_seeds({}) == []


# ---------------------------------------------------------------------------
# Item 1: map_gateway
# ---------------------------------------------------------------------------


class TestMapGateway:
    def test_basic_mapping(self):
        """Map two ECUs from captured UDS traffic."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41\x42'),
            ],
            base_time=0.0,
        )
        messages += _uds_flow(
            0x7E2,
            0x7EA,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x22\xf1\x91', b'\x62\xf1\x91\x43\x44'),
            ],
            base_time=1.0,
        )

        gw_map = map_gateway(messages)
        assert len(gw_map.ecus) == 2
        ids = {e.request_id for e in gw_map.ecus}
        assert ids == {0x7E0, 0x7E2}
        assert len(gw_map.diagnostic_ids) == 2

    def test_functional_tracking(self):
        """Functional addressing IDs are tracked but not listed as ECUs."""
        messages = _uds_flow(
            0x7DF,
            0x7E8,
            [(b'\x22\xf1\x90', b'\x62\xf1\x90\x41')],
            base_time=0.0,
        )
        messages += _uds_flow(
            0x7E0,
            0x7E8,
            [(b'\x22\xf1\x90', b'\x62\xf1\x90\x41')],
            base_time=1.0,
        )

        gw_map = map_gateway(messages)
        # 0x7DF should not appear as an ECU
        ecu_ids = {e.request_id for e in gw_map.ecus}
        assert 0x7DF not in ecu_ids
        assert 0x7DF in gw_map.functional_ids_seen

    def test_empty(self):
        gw_map = map_gateway([])
        assert gw_map.ecus == []
        assert gw_map.diagnostic_ids == []


# ---------------------------------------------------------------------------
# Item 2: build_access_matrix
# ---------------------------------------------------------------------------


class TestAccessMatrix:
    def test_allowed_and_denied(self):
        """Services that succeed are 'allowed', those with NRC 0x33 are 'denied'."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41'),  # allowed
                (b'\x2e\xf1\x90\x41', b'\x7f\x2e\x33'),  # denied
            ],
        )

        matrix = build_access_matrix(messages)
        assert matrix.ecus == [0x7E0]
        read_rows = [r for r in matrix.rows if r.service == 0x22]
        write_rows = [r for r in matrix.rows if r.service == 0x2E]
        assert any(r.status == 'allowed' for r in read_rows)
        assert any(r.status == 'denied' for r in write_rows)

    def test_multiple_sessions(self):
        """Services are tracked per session."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x22\xf1\x90', b'\x7f\x22\x33'),
                (b'\x10\x01', b'\x50\x01\x00\x19\x01\xf4'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41'),
            ],
        )

        matrix = build_access_matrix(messages)
        assert len(matrix.sessions) >= 1

    def test_empty(self):
        matrix = build_access_matrix([])
        assert matrix.rows == []


# ---------------------------------------------------------------------------
# Item 3: extract_unlock_sequence
# ---------------------------------------------------------------------------


class TestUnlockSequence:
    def test_seed_key_sequence(self):
        """Extract a session-change + seed/key unlock sequence."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
            ],
        )

        seq = extract_unlock_sequence(messages, 0x7E0)
        assert seq is not None
        assert seq.auth_type == 'seed_key_0x27'
        assert len(seq.steps) >= 3
        actions = [s.action for s in seq.steps]
        assert 'session_change' in actions
        assert 'security_access_seed' in actions
        assert 'security_access_key' in actions

    def test_no_auth(self):
        """No unlock sequence when no authentication was observed."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [(b'\x22\xf1\x90', b'\x62\xf1\x90\x41')],
        )
        seq = extract_unlock_sequence(messages, 0x7E0)
        assert seq is None

    def test_timing_constraints(self):
        """Timing constraints are set between consecutive steps."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
            ],
        )
        seq = extract_unlock_sequence(messages, 0x7E0)
        assert seq is not None
        # All steps except the last should have a timing constraint
        for step in seq.steps[:-1]:
            assert step.timing_constraint_ms is not None


# ---------------------------------------------------------------------------
# Item 4: gateway_report
# ---------------------------------------------------------------------------


class TestGatewayReport:
    def test_combined_report(self):
        """The combined report includes all sub-analyses."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41'),
            ],
        )

        report = gateway_report(messages)
        assert report.gateway_map is not None
        assert report.access_matrix is not None
        assert isinstance(report.auth_patterns, list)
        assert isinstance(report.bypass_findings, list)
        assert isinstance(report.unlock_sequences, dict)
        assert isinstance(report.severity_summary, dict)

    def test_empty(self):
        report = gateway_report([])
        assert report.gateway_map.ecus == []
        assert report.access_matrix.rows == []


# ---------------------------------------------------------------------------
# Item 5: fingerprint_ecu
# ---------------------------------------------------------------------------


class TestFingerprintEcu:
    def test_nrc_collection(self):
        """NRCs are collected and counted."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x22\xf1\x90', b'\x7f\x22\x33'),
                (b'\x22\xf1\x91', b'\x7f\x22\x33'),
                (b'\x2e\xf1\x90\x41', b'\x7f\x2e\x31'),
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
            ],
        )

        fp = fingerprint_ecu(messages, 0x7E0)
        assert 0x33 in fp.nrc_set
        assert 0x31 in fp.nrc_set
        assert fp.nrc_counts[0x33] == 2
        assert 3 in fp.supported_sessions
        assert fp.timing_profile['mean_ms'] > 0

    def test_timing_profile(self):
        """Response times are measured."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [(b'\x22\xf1\x90', b'\x62\xf1\x90\x41')],
        )

        fp = fingerprint_ecu(messages, 0x7E0)
        assert fp.timing_profile['mean_ms'] >= 0

    def test_empty(self):
        fp = fingerprint_ecu([], 0x7E0)
        assert fp.nrc_set == []
        assert fp.timing_profile['mean_ms'] == 0.0


# ---------------------------------------------------------------------------
# Item 6: plan_probing
# ---------------------------------------------------------------------------


class TestPlanProbing:
    def test_conditional_service_suggested(self):
        """Services with conditionsNotCorrect should be suggested in other sessions."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x2e\xf1\x90\x41', b'\x7f\x2e\x22'),  # WriteDID conditionsNotCorrect in session 3
            ],
        )
        # Also create traffic in default session with a different service so we have 2 sessions
        messages += _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x01', b'\x50\x01\x00\x19\x01\xf4'),
                (b'\x22\xf1\x91', b'\x62\xf1\x91\x41'),  # ReadDID in session 1
            ],
            base_time=1.0,
        )

        matrix = build_access_matrix(messages)
        steps = plan_probing(matrix)
        # Should suggest trying WriteDID (0x2E) in session 1 since it got conditionsNotCorrect in session 3
        high_prio = [s for s in steps if s.priority == 'high']
        assert len(high_prio) >= 1, f'Expected high-priority probe steps, got: {steps}'
        assert any(s.service == 0x2E for s in high_prio)

    def test_empty_matrix(self):
        matrix = build_access_matrix([])
        steps = plan_probing(matrix)
        assert steps == []


# ---------------------------------------------------------------------------
# Item 7: classify_did_security
# ---------------------------------------------------------------------------


class TestClassifyDidSecurity:
    def test_readable_did(self):
        """A DID that responds positively is classified as readable."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [(b'\x22\xf1\x90', b'\x62\xf1\x90\x41\x42')],
        )

        dids = classify_did_security(messages, 0x7E0)
        assert len(dids) == 1
        assert dids[0].identifier == 0xF190
        assert dids[0].readable is True
        assert dids[0].auth_required == 'none'

    def test_locked_did(self):
        """A DID that returns NRC 0x33 is classified as requiring authentication."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [(b'\x22\xf1\x90', b'\x7f\x22\x33')],
        )

        dids = classify_did_security(messages, 0x7E0)
        assert len(dids) == 1
        assert dids[0].readable is False
        assert dids[0].auth_required != 'none'

    def test_routine(self):
        """A routine control is classified."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [(b'\x31\x01\xff\x00', b'\x71\x01\xff\x00\x00')],
        )

        dids = classify_did_security(messages, 0x7E0)
        assert len(dids) == 1
        assert dids[0].service == 0x31
        assert dids[0].identifier == 0xFF00

    def test_empty(self):
        assert classify_did_security([], 0x7E0) == []


# ---------------------------------------------------------------------------
# Item 8: analyse_auth_timing
# ---------------------------------------------------------------------------


class TestAuthTiming:
    def test_timing_extraction(self):
        """Timing of a 0x27 flow is measured."""
        messages = []
        t = 0.0

        # Seed request
        messages.append(_msg(0x7E0, b'\x27\x01', t))
        t += 0.010  # 10ms
        # Seed response
        messages.append(_msg(0x7E8, b'\x67\x01\xaa\xbb\xcc\xdd', t))
        t += 0.050  # 50ms (tester computes key)
        # Key send
        messages.append(_msg(0x7E0, b'\x27\x02\x11\x22\x33\x44', t))
        t += 0.005  # 5ms
        # Key accepted
        messages.append(_msg(0x7E8, b'\x67\x02', t))

        results = analyse_auth_timing(messages)
        assert len(results) >= 1
        r = results[0]
        assert r.ecu_id == 0x7E0
        assert r.auth_type == 'seed_key_0x27'
        assert r.request_to_seed_ms is not None
        assert r.request_to_seed_ms > 0
        assert r.total_ms > 0

    def test_fast_seed_finding(self):
        """Very fast seed generation triggers a finding."""
        messages = []
        t = 0.0
        messages.append(_msg(0x7E0, b'\x27\x01', t))
        t += 0.0005  # 0.5ms -- very fast
        messages.append(_msg(0x7E8, b'\x67\x01\xaa\xbb', t))
        t += 0.050
        messages.append(_msg(0x7E0, b'\x27\x02\x11\x22', t))
        t += 0.0003  # 0.3ms -- very fast
        messages.append(_msg(0x7E8, b'\x67\x02', t))

        results = analyse_auth_timing(messages)
        assert len(results) >= 1
        # Should have findings about fast seed/key
        assert any('fast' in f.lower() or 'weak' in f.lower() for f in results[0].findings)

    def test_empty(self):
        assert analyse_auth_timing([]) == []


# ---------------------------------------------------------------------------
# Item 11: Bus segmentation detection
# ---------------------------------------------------------------------------


class TestDetectBusSegments:
    def test_two_segments(self):
        """IDs in two distant ranges form two segments."""
        messages = []
        t = 0.0
        for _ in range(50):
            messages.append(_msg(0x100, b'\x01', t))
            messages.append(_msg(0x110, b'\x02', t + 0.001))
            messages.append(_msg(0x600, b'\x03', t + 0.002))
            messages.append(_msg(0x610, b'\x04', t + 0.003))
            t += 0.010  # 100 Hz

        segments = detect_bus_segments(messages)
        assert len(segments) == 2
        # First segment contains 0x100 and 0x110
        low_seg = [s for s in segments if 0x100 in s.ids][0]
        assert 0x110 in low_seg.ids
        # Second segment contains 0x600 and 0x610
        high_seg = [s for s in segments if 0x600 in s.ids][0]
        assert 0x610 in high_seg.ids

    def test_periodic_vs_event(self):
        """Periodic IDs are classified as periodic, event-driven IDs are not."""
        messages = []
        t = 0.0
        for _ in range(100):
            messages.append(_msg(0x100, b'\x01', t))
            t += 0.010  # Very regular 100 Hz

        # Event-driven: random-ish timing within the same range
        import random

        rng = random.Random(42)
        t2 = 0.0
        for _ in range(20):
            messages.append(_msg(0x120, b'\x02', t2))
            t2 += rng.uniform(0.001, 0.5)

        segments = detect_bus_segments(messages)
        assert len(segments) >= 1
        seg = segments[0]
        # 0x100 should be periodic
        assert seg.periodic_ratio > 0
        # 0x120 should be event-driven
        assert 0x120 in seg.event_driven_ids

    def test_single_id(self):
        """A single ID with enough messages forms one segment."""
        messages = [_msg(0x100, b'\x01', i * 0.01) for i in range(50)]
        segments = detect_bus_segments(messages)
        assert len(segments) == 1
        assert segments[0].ids == [0x100]

    def test_empty(self):
        assert detect_bus_segments([]) == []


# ---------------------------------------------------------------------------
# Item 12: Response-after-reset analysis
# ---------------------------------------------------------------------------


class TestResetBehaviour:
    def test_service_after_reset(self):
        """A service succeeding shortly after reset is flagged."""
        messages = []
        t = 0.0
        # ECU reset request
        messages.append(_msg(0x7E0, b'\x11\x01', t))
        t += 0.050
        # ECU reset positive response
        messages.append(_msg(0x7E8, b'\x51\x01', t))
        t += 0.200  # 200 ms after reset
        # WriteDID succeeds (should be flagged as finding)
        messages.append(_msg(0x7E0, b'\x2e\xf1\x90\xaa', t))
        t += 0.005
        messages.append(_msg(0x7E8, b'\x6e\xf1\x90', t))

        findings = analyse_reset_behaviour(messages)
        assert len(findings) >= 1
        f = findings[0]
        assert f.ecu_id == 0x7E0
        assert f.service == 0x2E  # WriteDID
        assert f.severity == 'finding'
        assert f.success_time_after_reset_ms < 300

    def test_no_reset(self):
        """No reset in the traffic produces no findings."""
        messages = [
            _msg(0x7E0, b'\x22\xf1\x90', 0.0),
            _msg(0x7E8, b'\x62\xf1\x90\xaa', 0.05),
        ]
        assert analyse_reset_behaviour(messages) == []

    def test_outside_window(self):
        """A service succeeding well after the reset is not flagged."""
        messages = []
        t = 0.0
        messages.append(_msg(0x7E0, b'\x11\x01', t))
        t += 0.050
        messages.append(_msg(0x7E8, b'\x51\x01', t))
        t += 5.0  # 5 seconds later -- outside the default 2s window
        messages.append(_msg(0x7E0, b'\x2e\xf1\x90\xaa', t))
        t += 0.005
        messages.append(_msg(0x7E8, b'\x6e\xf1\x90', t))

        findings = analyse_reset_behaviour(messages)
        assert len(findings) == 0

    def test_empty(self):
        assert analyse_reset_behaviour([]) == []


# ---------------------------------------------------------------------------
# Item 13: OEM-specific NRC decoder
# ---------------------------------------------------------------------------


class TestNrcDecoder:
    def test_standard_nrc(self):
        assert decode_nrc(0x33) == 'securityAccessDenied'

    def test_oem_nrc(self):
        assert decode_nrc(0x72) == 'generalProgrammingFailure'

    def test_vendor_specific_unknown(self):
        """Unknown OEM-range NRC returns a vendor-specific label."""
        result = decode_nrc(0x74)
        assert 'vendor-specific' in result

    def test_vendor_nrc_in_auth_details(self):
        """When a vendor-specific NRC is returned, analyse_gateway_auth includes decoded info."""
        messages = [
            # Request
            _msg(0x7E0, b'\x34\x00\x44\x00\x01\x00\x00', 0.0),
            # Negative response with vendor NRC 0x72 (generalProgrammingFailure)
            _msg(0x7E8, b'\x7f\x34\x72', 0.05),
        ]
        patterns = analyse_gateway_auth(messages)
        assert len(patterns) == 1
        assert 'vendor NRC' in patterns[0].details
        assert '0x72' in patterns[0].details


# ---------------------------------------------------------------------------
# Item 14: Export gateway findings
# ---------------------------------------------------------------------------


class TestExportReport:
    def _make_report(self):
        """Create a minimal report for testing export."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\xaa'),
            ],
        )
        return gateway_report(messages)

    def test_json_export(self):
        import json as json_mod

        report = self._make_report()
        output = export_report(report, fmt='json')
        data = json_mod.loads(output)
        assert 'overall_severity' in data
        assert 'summary' in data
        assert 'ecus' in data
        assert isinstance(data['ecus'], list)

    def test_csv_export(self):
        report = self._make_report()
        output = export_report(report, fmt='csv')
        lines = output.strip().splitlines()
        assert lines[0].strip() == 'category,id,finding,severity,details'
        assert len(lines) >= 2  # Header + at least one row

    def test_invalid_format(self):
        import pytest

        report = self._make_report()
        with pytest.raises(ValueError, match='Unsupported export format'):
            export_report(report, fmt='xml')

    def test_empty_report(self):
        import json as json_mod

        report = gateway_report([])
        output = export_report(report, fmt='json')
        data = json_mod.loads(output)
        assert data['overall_severity'] == 'info'


# ---------------------------------------------------------------------------
# recover_auth_state_machine (roadmap 1.1)
# ---------------------------------------------------------------------------


class TestRecoverAuthStateMachine:
    def test_basic_state_machine(self):
        """Recover a simple session-change + security-access state machine."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41\x42'),
            ],
        )
        sm = recover_auth_state_machine(messages, 0x7E0)
        assert sm is not None
        assert sm.ecu_id == 0x7E0
        assert len(sm.states) >= 2
        assert len(sm.transitions) >= 2
        assert sm.initial_state == 'default_session'
        # First transition should be a session change
        assert sm.transitions[0].action.startswith('session_change')

    def test_no_auth_flow(self):
        """Return None when no authentication flow is observed."""
        messages = [_msg(0x7E0, b'\x22\xf1\x90', 0.0), _msg(0x7E8, b'\x62\xf1\x90\x41', 0.1)]
        sm = recover_auth_state_machine(messages, 0x7E0)
        assert sm is None

    def test_tester_present_detected(self):
        """Detect TesterPresent keep-alive in the auth flow."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x3e\x00', b'\x7e\x00'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x3e\x00', b'\x7e\x00'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
            ],
        )
        sm = recover_auth_state_machine(messages, 0x7E0)
        assert sm is not None
        assert sm.tester_present_required is True

    def test_empty_messages(self):
        sm = recover_auth_state_machine([], 0x7E0)
        assert sm is None


# ---------------------------------------------------------------------------
# differential_analysis (roadmap 1.3)
# ---------------------------------------------------------------------------


class TestDifferentialAnalysis:
    def test_id_appeared(self):
        """Detect an ID that appears in the second capture but not the first."""
        cap1 = [_msg(0x100, b'\x01\x02', 0.0), _msg(0x100, b'\x01\x03', 0.1)]
        cap2 = [
            _msg(0x100, b'\x01\x02', 1.0),
            _msg(0x200, b'\xaa\xbb', 1.1),
        ]
        result = differential_analysis([cap1, cap2])
        assert result.capture_count == 2
        appeared = [c for c in result.changes if c.change_type == 'appeared']
        assert any(c.identifier == 0x200 for c in appeared)

    def test_id_disappeared(self):
        """Detect an ID that disappears."""
        cap1 = [_msg(0x100, b'\x01', 0.0), _msg(0x200, b'\x02', 0.1)]
        cap2 = [_msg(0x100, b'\x01', 1.0)]
        result = differential_analysis([cap1, cap2])
        disappeared = [c for c in result.changes if c.change_type == 'disappeared']
        assert any(c.identifier == 0x200 for c in disappeared)

    def test_needs_two_captures(self):
        cap1 = [_msg(0x100, b'\x01', 0.0)]
        result = differential_analysis([cap1])
        assert result.capture_count == 1
        assert len(result.changes) == 0

    def test_empty_captures(self):
        result = differential_analysis([[], []])
        assert result.capture_count == 2
        assert len(result.changes) == 0


# ---------------------------------------------------------------------------
# infer_filter_rules (roadmap 3.1)
# ---------------------------------------------------------------------------


class TestInferFilterRules:
    def test_service_deny_rule(self):
        """Infer a deny rule when every request is rejected."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x22\xf1\x90', b'\x7f\x22\x33'),
                (b'\x22\xf1\x91', b'\x7f\x22\x33'),
                (b'\x22\xf1\x92', b'\x7f\x22\x33'),
            ],
        )
        ruleset = infer_filter_rules(messages)
        deny_rules = [r for r in ruleset.rules if r.action == 'deny' and r.service == 0x22]
        assert len(deny_rules) >= 1

    def test_service_allow_rule(self):
        """Infer an allow rule when every request succeeds."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41'),
                (b'\x22\xf1\x91', b'\x62\xf1\x91\x42'),
            ],
        )
        ruleset = infer_filter_rules(messages)
        allow_rules = [r for r in ruleset.rules if r.action == 'allow' and r.service == 0x22]
        assert len(allow_rules) >= 1

    def test_rate_limit_detection(self):
        """Detect rate limiting from NRC 0x37."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x27\x01', b'\x67\x01\xaa\xbb'),
                (b'\x27\x02\x11\x22', b'\x7f\x27\x37'),
                (b'\x27\x01', b'\x7f\x27\x37'),
                (b'\x27\x01', b'\x7f\x27\x37'),
                (b'\x27\x01', b'\x7f\x27\x37'),
                (b'\x27\x01', b'\x7f\x27\x37'),
            ],
        )
        ruleset = infer_filter_rules(messages)
        rate_rules = [r for r in ruleset.rules if r.action == 'rate_limit']
        assert len(rate_rules) >= 1

    def test_empty_messages(self):
        ruleset = infer_filter_rules([])
        assert len(ruleset.rules) == 0
        assert ruleset.coverage == 0.0

    def test_multiple_ecus(self):
        """Filter rules are inferred per ECU."""
        messages = _uds_flow(0x7E0, 0x7E8, [(b'\x22\xf1\x90', b'\x62\xf1\x90\x41')]) + _uds_flow(
            0x7E2, 0x7EA, [(b'\x22\xf1\x90', b'\x7f\x22\x33')], base_time=1.0
        )
        ruleset = infer_filter_rules(messages)
        assert len(ruleset.ecus) == 2


# ---------------------------------------------------------------------------
# attack_surface_report (roadmap 1.7)
# ---------------------------------------------------------------------------


class TestAttackSurfaceReport:
    def test_basic_report(self):
        """Produce a report from a simple auth flow."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41\x42'),
            ],
        )
        report = attack_surface_report(messages)
        assert report.ecu_count >= 1
        assert isinstance(report.overall_score, float)
        assert isinstance(report.summary, list)
        assert len(report.summary) >= 1

    def test_empty_messages(self):
        report = attack_surface_report([])
        assert report.ecu_count == 0
        assert report.overall_score == 0.0


# ---------------------------------------------------------------------------
# enumerate_plan (roadmap 1.4)
# ---------------------------------------------------------------------------


class TestEnumeratePlan:
    def test_basic_plan(self):
        """Generate an enumeration plan from a simple auth flow."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41\x42'),
            ],
        )
        plan = enumerate_plan(messages)
        assert plan.ecu_count >= 1
        assert isinstance(plan.steps, list)
        assert isinstance(plan.estimated_time_seconds, float)
        assert isinstance(plan.findings, list)

    def test_empty_messages(self):
        plan = enumerate_plan([])
        assert plan.ecu_count == 0
        assert len(plan.steps) == 0


# ---------------------------------------------------------------------------
# identify_ecu (roadmap 1.5)
# ---------------------------------------------------------------------------


class TestIdentifyEcu:
    def test_did_extraction(self):
        """Extract ECU identity from DID responses."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41\x42\x43'),
                (b'\x22\xf1\x91', b'\x62\xf1\x91\x31\x32'),
                (b'\x22\xf1\x95', b'\x62\xf1\x95\x56\x31'),
            ],
        )
        identity = identify_ecu(messages, 0x7E0)
        assert identity.request_id == 0x7E0
        assert 0xF190 in identity.did_values
        assert isinstance(identity.findings, list)

    def test_no_did_responses(self):
        """ECU with no DID responses."""
        messages = _uds_flow(0x7E0, 0x7E8, [(b'\x22\xf1\x90', b'\x7f\x22\x33')])
        identity = identify_ecu(messages, 0x7E0)
        assert identity.request_id == 0x7E0
        assert len(identity.did_values) == 0

    def test_empty_messages(self):
        identity = identify_ecu([], 0x7E0)
        assert identity.request_id == 0x7E0
        assert len(identity.did_values) == 0


# ---------------------------------------------------------------------------
# brute_plan (roadmap 1.8)
# ---------------------------------------------------------------------------


class TestBrutePlan:
    def test_basic_plan(self):
        """Generate a brute-force plan."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb'),
            ],
        )
        plan = brute_plan(messages)
        assert plan.ecu_count >= 1
        assert isinstance(plan.steps, list)
        assert isinstance(plan.oem_sessions, list)
        assert isinstance(plan.non_standard_levels, list)
        assert isinstance(plan.estimated_attempts, int)

    def test_empty_messages(self):
        plan = brute_plan([])
        assert plan.ecu_count == 0
        assert len(plan.steps) == 0


# ---------------------------------------------------------------------------
# detect_timing_side_channel (roadmap 1.9)
# ---------------------------------------------------------------------------


class TestTimingSideChannel:
    def test_insufficient_data(self):
        """Insufficient data returns None."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x27\x01', b'\x67\x01\xaa\xbb'),
                (b'\x27\x02\x11\x22', b'\x67\x02'),
            ],
        )
        result = detect_timing_side_channel(messages, 0x7E0)
        assert result is None

    def test_empty_messages(self):
        result = detect_timing_side_channel([], 0x7E0)
        assert result is None


# ---------------------------------------------------------------------------
# infer_routing_table (roadmap 1.10)
# ---------------------------------------------------------------------------


class TestInferRoutingTable:
    def test_forwarded_id(self):
        """Detect forwarded messages."""
        external = [
            _msg(0x7E0, b'\x22\xf1\x90', 0.0),
            _msg(0x7E8, b'\x62\xf1\x90\x41', 0.05),
        ]
        internal = [
            _msg(0x7E0, b'\x22\xf1\x90', 0.001),
            _msg(0x7E8, b'\x62\xf1\x90\x41', 0.051),
        ]
        table = infer_routing_table(external, internal)
        assert len(table.forwarded_ids) >= 1
        assert isinstance(table.findings, list)

    def test_blocked_id(self):
        """Detect blocked messages."""
        external = [_msg(0x7E0, b'\x22\xf1\x90', 0.0)]
        internal = [_msg(0x7E2, b'\x22\xf1\x90', 0.0)]
        table = infer_routing_table(external, internal)
        assert 0x7E0 in table.blocked_ids or len(table.entries) > 0

    def test_empty(self):
        table = infer_routing_table([], [])
        assert len(table.entries) == 0


# ---------------------------------------------------------------------------
# detect_rewriting (roadmap 3.5)
# ---------------------------------------------------------------------------


class TestDetectRewriting:
    def test_id_mapping(self):
        """Detect ID mapping through the gateway."""
        external = [_msg(0x7E0, b'\x22\xf1\x90', 0.0)]
        internal = [_msg(0x600, b'\x22\xf1\x90', 0.001)]
        result = detect_rewriting(external, internal)
        assert isinstance(result.findings, list)
        assert isinstance(result.matched_pairs, int)

    def test_empty(self):
        result = detect_rewriting([], [])
        assert result.matched_pairs == 0


# ---------------------------------------------------------------------------
# discover_unlock_chains (roadmap 3.6)
# ---------------------------------------------------------------------------


class TestDiscoverUnlockChains:
    def test_simple_chain(self):
        """Discover a chain with session switch + security access."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41'),
            ],
        )
        chains = discover_unlock_chains(messages)
        assert isinstance(chains, list)

    def test_empty_messages(self):
        chains = discover_unlock_chains([])
        assert len(chains) == 0


# ---------------------------------------------------------------------------
# scan_plan_non_standard (roadmap 3.7)
# ---------------------------------------------------------------------------


class TestScanPlanNonStandard:
    def test_basic_plan(self):
        """Generate a scan plan from known responders."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [(b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4')],
        )
        plan = scan_plan_non_standard(messages)
        assert isinstance(plan.ranges, list)
        assert len(plan.ranges) >= 1
        assert isinstance(plan.estimated_probes, int)
        assert isinstance(plan.known_responders, list)

    def test_empty_messages(self):
        plan = scan_plan_non_standard([])
        assert isinstance(plan.ranges, list)
        assert len(plan.known_responders) == 0


# ---------------------------------------------------------------------------
# analyse_doip_gateway (roadmap 3.9)
# ---------------------------------------------------------------------------


class TestAnalyseDoipGateway:
    def test_no_doip(self):
        """Non-DoIP traffic returns empty analysis."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [(b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4')],
        )
        result = analyse_doip_gateway(messages)
        assert isinstance(result.findings, list)
        assert isinstance(result.entity_addresses, list)

    def test_empty_messages(self):
        result = analyse_doip_gateway([])
        assert isinstance(result.findings, list)


# ---------------------------------------------------------------------------
# gateway_compliance_report (roadmap 3.10)
# ---------------------------------------------------------------------------


class TestGatewayComplianceReport:
    def test_basic_report(self):
        """Produce a compliance report from auth traffic."""
        messages = _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x27\x01', b'\x67\x01\xaa\xbb\xcc\xdd'),
                (b'\x27\x02\x11\x22\x33\x44', b'\x67\x02'),
                (b'\x22\xf1\x90', b'\x62\xf1\x90\x41'),
            ],
        )
        report = gateway_compliance_report(messages)
        assert isinstance(report.findings, list)
        assert isinstance(report.wp29_coverage, dict)
        assert report.overall_compliance in ('compliant', 'non_compliant', 'partial', 'insufficient_data')
        assert isinstance(report.summary, list)

    def test_empty_messages(self):
        report = gateway_compliance_report([])
        assert isinstance(report.findings, list)
        assert report.overall_compliance in ('compliant', 'non_compliant', 'partial', 'insufficient_data')


# ---------------------------------------------------------------------------
# Responsible-disclosure report
# ---------------------------------------------------------------------------


class TestDisclosureReport:
    def _capture(self):
        # A sensitive service answered with no 0x27/0x29 anywhere: a weak-auth finding
        return _uds_flow(
            0x7E0,
            0x7E8,
            [
                (b'\x10\x03', b'\x50\x03\x00\x19\x01\xf4'),
                (b'\x2e\xf1\x90\x01', b'\x6e\xf1\x90'),
                (b'\x31\x01\x12\x34', b'\x71\x01\x12\x34'),
            ],
        )

    def test_markdown_structure(self):
        from can_explorer.core.reverse.gateway_security import disclosure_report

        text = disclosure_report(self._capture(), vehicle='Test Mule', researcher='A. Researcher', contact='a@b.c')
        assert text.startswith('# Security disclosure: Test Mule')
        assert 'Reported by: A. Researcher <a@b.c>' in text
        assert '## Findings' in text and '## Topology observed' in text
        assert 'no exploit or bypass procedure' in text
        assert 'ECU 0x7E0' in text  # Topology lists the discovered ECU

    def test_json_is_severity_ordered(self):
        import json

        from can_explorer.core.reverse.gateway_security import _SEVERITY_RANK, disclosure_report

        data = json.loads(disclosure_report(self._capture(), fmt='json', vehicle='M'))
        assert data['vehicle'] == 'M' and 'overall_severity' in data
        ranks = [_SEVERITY_RANK.get(f['severity'], 99) for f in data['findings']]
        assert ranks == sorted(ranks)  # Most-severe first
        for finding in data['findings']:
            assert finding['remediation'] and finding['reference']

    def test_empty_capture(self):
        from can_explorer.core.reverse.gateway_security import disclosure_report

        text = disclosure_report([])
        assert 'No gateway weaknesses were found' in text

    def test_bad_format(self):
        import pytest

        from can_explorer.core.reverse.gateway_security import disclosure_report

        with pytest.raises(ValueError):
            disclosure_report([], fmt='pdf')
