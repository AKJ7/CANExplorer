# Security Reverse Engineering Roadmap

!!! success "Completed"
    All features in this roadmap have been implemented as of November 2026.
    The document is kept as a reference for the design rationale and
    dependencies of each feature. It is no longer a to-do list.

    **Core modules**: `core/reverse/gateway_security.py`, `core/reverse/crypto.py`,
    `core/active_security_test.py`, `core/reverse/linear.py`, `core/secoc.py`,
    `core/secoc_check.py`, `core/seedkey_crack.py`, `core/reverse/synthetic.py`.
    **GUI**: `GatewaySecurityPanel.qml`, `ReverseCryptoPanel.qml`,
    `SecOcReversePanel.qml`, `SecurityDashboard.qml`.
    **MCP**: all tools listed below are registered in `mcp/server.py`.
    **Tests**: `tests/unit/core/test_gateway_security.py`,
    `tests/unit/core/test_crypto_analysis.py`,
    `tests/unit/core/test_synthetic_security.py`.
    **Docs**: [Reverse engineering](../user-guide/reverse-engineering.md),
    [SecOC reverse engineering](../user-guide/secoc-reverse-engineering.md),
    [Security manager](../user-guide/security-manager.md).

Features for car security reverse engineering, SecOC reverse engineering
and security gateway bypassing. Each feature lists where it lives in the
codebase, what it depends on, and its priority (P0 = must-have, P1 = important,
P2 = nice-to-have).

**Scope**: legitimate automotive security research and testing on authorised
benches. Active tests (anything that transmits) are gated behind
`--allow-send` / `--allow-control` and are never enabled by default.

---

## Existing foundation

The following capabilities are already built (new, uncommitted) and form the
base this roadmap extends:

### Passive gateway analysis (`core/reverse/gateway_security.py`)

- Gateway authentication pattern detection (0x27 / 0x29)
- Gateway filtering weakness analysis (unfiltered IDs, session fallback,
  timing windows, functional addressing bypass)
- Cross-ECU seed/key correlation
- Automated gateway topology mapping (response-time clustering)
- UDS service access matrix (per-ECU, per-service, per-session)
- Unlock sequence extraction (exact authentication recipe)
- NRC fingerprinting and ECU identification (vendor NRC patterns, timing)
- Auth flow timing analysis (request-to-seed, seed-to-key, key-to-response)
- DID/routine security classification
- Multi-session probing planner
- Bus segmentation detection
- Response-after-reset analysis (boot-up bypass windows)
- OEM-specific NRC decoder (public sources only)
- Export to JSON / CSV

### Cryptanalysis (`core/reverse/crypto.py`)

- Statistical randomness tests (monobit, runs, chi-square, serial correlation)
- Freshness / replay analysis (counter wraps, MAC-on-wrap, forgery time)
- MAC pattern classification (truncation length, SecOC CMAC heuristics)
- Keystream reuse detection (repeated counter values, XOR structure)

### Seed/key cracking (`core/seedkey_crack.py`)

- Identity, constant XOR, constant ADD/SUB, per-byte XOR/ADD
- Bit rotation, byte shuffle, per-byte lookup table
- GF(2) linear transform (Gaussian elimination over bit matrices)
- AES-based KDF with candidate keys (AES-ECB, CMAC-AES, HMAC-SHA-256)
- LFSR-based transforms (Berlekamp-Massey)
- Seed entropy / quality assessment

### Active security tests (`core/active_security_test.py`)

- Frame replay test (send tier)
- MAC forgery test (random MAC bytes, send tier)

### Linear dependency search (`core/reverse/linear.py`)

- GF(2) Gaussian elimination for affine bit relations
- Known CRC polynomial matching (AUTOSAR E2E library)

### SecOC verification (`core/secoc.py`, `core/secoc_check.py`)

- CMAC-AES-128 and HMAC-SHA-256 MAC generation and verification
- Bit-packed trailer splitting (freshness + MAC)
- Live freshness counter reconstruction and replay detection
- ARXML-driven SecOC configuration
- Key store integration

### GUI

- `GatewaySecurityPanel.qml` — topology, auth patterns, access matrix, bypass
  findings, seed/key cracking, export
- `ReverseCryptoPanel.qml` — randomness tests, freshness analysis, linear
  relations, keystream reuse per identifier

### MCP tools

- `assess_frame_security`, `crack_seed_key`, `assess_seed_quality`
- `analyse_gateway_auth`, `analyse_gateway_filtering`, `correlate_ecu_seeds`
- `map_gateway`, `access_matrix`, `extract_unlock_sequence`, `gateway_report`
- `classify_did_security`, `detect_bus_segments`, `analyse_reset_behaviour`
- `export_gateway_report`
- `replay_test`, `forgery_test` (send tier)

---

## 1. Car security reverse engineering (done)

Passive and active techniques for understanding a vehicle's security
architecture from captured bus traffic and controlled bench testing.

### 1.1 ECU authentication state machine recovery — P0

**What**: From captured UDS traffic, reconstruct the full state machine each
ECU expects for authentication — the required order of session changes,
security access levels, tester present keep-alive requirements and timing
constraints. Output a machine-readable recipe (JSON) and a human-readable
sequence diagram.

**Where**: `core/reverse/gateway_security.py` (new `recover_auth_state_machine`
function), `mcp/server.py` + `mcp/service.py` (new MCP tool
`auth_state_machine`).

**Depends on**: `extract_unlock_sequence` (exists), `analyse_auth_timing`
(exists, not yet exposed as MCP tool).

**Priority**: P0 — the existing unlock-sequence extractor captures the steps
but does not model the state machine (which steps are mandatory, which are
order-dependent, which have timing constraints).

### 1.2 Expose NRC fingerprinting, auth timing and probing planner as MCP tools — P0

**What**: Three functions in `gateway_security.py` are implemented but have no
MCP tool wrapper: `fingerprint_ecu`, `analyse_auth_timing` and `plan_probing`.
Wrap them in `mcp/server.py` and `mcp/service.py` so an AI agent can call them.

**Where**: `mcp/server.py`, `mcp/service.py`.

**Depends on**: The core functions exist.

**Priority**: P0 — low effort, high value. The probing planner is useless
without an MCP interface since it is meant for agent-driven exploration.

### 1.3 Multi-capture differential analysis — P0

**What**: Compare two or more captures (e.g. locked vs. unlocked ECU, engine
off vs. on, pre-auth vs. post-auth) to automatically surface which IDs,
services and payloads changed. For each changed identifier, report the
direction of the change (appeared / disappeared / payload delta) and whether
it correlates with the authentication event.

**Where**: `core/reverse/gateway_security.py` (new `differential_analysis`),
`mcp/service.py`, `mcp/server.py`.

**Depends on**: `collect` from `core/reverse/analysis.py`.

**Priority**: P0 — this is the fundamental technique for understanding what an
authentication event does on the bus.

### 1.4 UDS service enumeration planner — P1

**What**: Given the access matrix and probing plan, generate a minimal sequence
of UDS requests to enumerate every reachable service, DID and routine on each
ECU, respecting session and security-level dependencies. Output a step-by-step
script (JSON) the agent can execute with the existing `uds_raw` tool.

**Where**: `core/reverse/gateway_security.py` (new `enumerate_plan`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: `plan_probing` (exists), `access_matrix` (exists).

**Priority**: P1 — the probing planner suggests what to try; this produces the
concrete UDS script.

### 1.5 ECU firmware version fingerprinting — P1

**What**: Automatically read the standard UDS DIDs for ECU identification
(F186 active session, F187 spare part, F188 diagnosis name, F189 ECU serial,
F190 VIN, F191 hardware version, F193 system supplier, F195 software version)
and correlate the responses with the NRC fingerprint to build a full ECU
identity card. This is read-only (diagnostics tier).

**Where**: `core/reverse/gateway_security.py` (new `identify_ecu`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: `fingerprint_ecu` (exists), UDS `uds_read_did` (exists).

**Priority**: P1 — vendor identification helps guide further analysis (known
weaknesses per vendor stack).

### 1.6 CAN FD payload security assessment — P1

**What**: Extend `assess_frame_security` to handle the larger CAN FD payload
(up to 64 bytes). CAN FD frames can carry full 128-bit MACs and longer
freshness values. The randomness tests and MAC classifier need updated
thresholds and byte-range heuristics for the wider payloads.

**Where**: `core/reverse/crypto.py` (update `test_randomness`, `classify_mac`),
`mcp/service.py` (update `assess_frame_security`).

**Depends on**: Existing crypto module.

**Priority**: P1 — CAN FD is increasingly common and its security properties
differ from classic CAN.

### 1.7 Automated attack surface report — P1

**What**: Combine gateway report, access matrix, seed quality, NRC
fingerprints and auth timing into a single prioritised attack surface report
with a severity score per ECU and per finding. Output JSON, CSV and a
human-readable summary.

**Where**: `core/reverse/gateway_security.py` (new `attack_surface_report`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: `gateway_report` (exists), `assess_seed_quality` (exists),
`fingerprint_ecu` (exists), `analyse_auth_timing` (exists).

**Priority**: P1 — consolidates existing analyses into one actionable report.

### 1.8 Session and security-level brute-force planner — P2

**What**: For ECUs that support OEM-specific sessions (0x40-0x5F) or
non-standard security levels, generate a plan to systematically try each
combination. This is planning only (read-only); execution uses existing UDS
tools in the control/send tier.

**Where**: `core/reverse/gateway_security.py` (new `brute_plan`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: `access_matrix` (exists), `plan_probing` (exists).

**Priority**: P2 — only needed for ECUs with non-standard session/level
schemes.

### 1.9 Timing side-channel analysis — P2

**What**: Extend `analyse_auth_timing` to detect timing side channels:
statistically significant differences in key-verification time that correlate
with individual key bytes. Report whether the ECU's key comparison appears to
be constant-time.

**Where**: `core/reverse/gateway_security.py` (extend `analyse_auth_timing`).

**Depends on**: `analyse_auth_timing` (exists). Requires many captured
seed/key attempts.

**Priority**: P2 — requires a large dataset and controlled conditions; rare in
practice but powerful when it works.

### 1.10 Diagnostic routing table inference — P2

**What**: From captured traffic across multiple bus segments (e.g. via two
interfaces), infer the gateway's routing table: which request IDs are forwarded
to which internal segment, and which are blocked or rewritten.

**Where**: `core/reverse/gateway_security.py` (new `infer_routing_table`).

**Depends on**: `map_gateway` (exists), `detect_bus_segments` (exists).
Requires captures from at least two bus segments.

**Priority**: P2 — most useful for multi-interface setups.

---

## 2. SecOC reverse engineering (done)

Reversing AUTOSAR Secure Onboard Communication: extracting MACs and freshness
values, assessing their strength, and recovering key material where possible.

### 2.1 Automated SecOC field extraction without ARXML — P0

**What**: When no ARXML is loaded, automatically detect SecOC-secured frames by
their statistical signature: a trailing block of random-looking bytes preceded
by a monotonic counter. Estimate the MAC length, freshness length and the
boundary between authentic payload and security trailer.

The existing `_random_trailer` in `analysis.py` labels random trailing bytes
but does not split them into freshness + MAC or confirm whether the counter is
a SecOC alive counter. This feature adds that classification.

**Where**: `core/reverse/crypto.py` (new `detect_secoc_layout`),
`core/reverse/analysis.py` (use it to refine the `random` kind into
`freshness` + `mac` when no ARXML is present).

**Depends on**: `test_randomness` (exists), `classify_mac` (exists),
`analyse_freshness` (exists).

**Priority**: P0 — the most common starting point: "I have a capture and no
ARXML, which frames are SecOC-protected?"

### 2.2 Freshness value reconstruction and sync analysis — P0

**What**: For each detected or ARXML-labelled SecOC frame, reconstruct the full
freshness value from the truncated bits transmitted on the bus:

- Detect the counter width (4, 8, 12, 16, 24 bits) and wrap behaviour.
- Identify the freshness manager type: monotonic counter, trip/reset counter
  or time-based.
- Detect synchronisation messages (FreshnessValue master frames that reset
  or update the counter on multiple PDUs simultaneously).
- Report the effective freshness window (how many frames before a counter
  wraps and old frames become replayable).

**Where**: `core/reverse/crypto.py` (new `reconstruct_freshness`,
`detect_sync_messages`).

**Depends on**: `analyse_freshness` (exists, does counter wraps and replay
assessment but not reconstruction or sync detection).

**Priority**: P0 — understanding the freshness scheme is the prerequisite for
all further SecOC analysis.

### 2.3 MAC truncation strength assessment — P0

**What**: For each SecOC frame, compute the practical security margin:

- Forgery probability per attempt (2^-MAC_bits).
- Expected time to forgery at the observed frame rate.
- Whether the truncation length meets AUTOSAR recommendations (24+ bits for
  safety-critical, 28+ for chassis, 32+ for powertrain).
- Cross-reference with the freshness window: if the counter wraps faster than
  the expected forgery time, replay + forgery is feasible.

Extend the existing `classify_mac` and `analyse_freshness` to produce this
combined assessment.

**Where**: `core/reverse/crypto.py` (new `secoc_strength_assessment`),
`mcp/server.py`, `mcp/service.py` (new MCP tool `assess_secoc_strength`).

**Depends on**: `classify_mac` (exists), `analyse_freshness` (exists).

**Priority**: P0 — the core deliverable of a SecOC audit.

### 2.4 Known-plaintext MAC key recovery — P1

**What**: When the authentic payload and freshness value are known (from an
ARXML or from the layout detector), try to recover the MAC key by testing
common / weak keys against the observed truncated MAC:

- All-zero, all-one, sequential and other common test keys (extends
  `COMMON_KEYS` from `seedkey_crack.py`).
- Keys derived from the Data ID (key = Data ID repeated).
- Keys derived from the ECU serial number (if read via DID F189).
- Short key-space brute force when the truncation is very short (16 bits:
  2^16 attempts per candidate key is feasible).

This is passive: it uses captured frames and known payloads, never sends.

**Where**: `core/reverse/crypto.py` (new `try_secoc_key_recovery`).

**Depends on**: `secoc.generate_mac` (exists), `secoc.verify` (exists),
`detect_secoc_layout` (2.1).

**Priority**: P1 — requires knowing the plaintext; most useful after ARXML
loading or layout detection.

### 2.5 Cross-PDU key sharing detection — P1

**What**: Detect whether multiple SecOC-protected PDUs share the same MAC key
by checking whether the same candidate key verifies MACs on different PDUs.
Shared keys mean compromising one PDU exposes all.

**Where**: `core/reverse/crypto.py` (new `detect_shared_secoc_keys`).

**Depends on**: `try_secoc_key_recovery` (2.4).

**Priority**: P1 — natural extension of key recovery; similar to the existing
`correlate_ecu_seeds` for 0x27.

### 2.6 Freshness value prediction and replay window calculator — P1

**What**: Given the reconstructed freshness scheme, predict the counter value
at a future time and compute the replay window: the time range during which a
captured frame can be successfully replayed (counter matches the receiver's
expected value).

Output: for each SecOC frame, the replay window in seconds/frames and the
predicted counter value at a given future timestamp.

**Where**: `core/reverse/crypto.py` (new `predict_freshness`,
`replay_window`).

**Depends on**: `reconstruct_freshness` (2.2).

**Priority**: P1 — directly answers "can I replay this frame at time T?"

### 2.7 SecOC bypass detection (authentication-free paths) — P1

**What**: Detect paths where SecOC protection is configured but not enforced:

- PDUs configured as secured in the ARXML but observed on the bus without a
  security trailer (shorter than expected).
- PDUs where the MAC is always the same regardless of the payload (MAC not
  checked by the receiver).
- PDUs accepted after the freshness counter wraps without rejection.
- Transition periods after ECU reset where SecOC is not yet active.

**Where**: `core/reverse/crypto.py` (new `detect_secoc_bypass`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: `analyse_freshness` (exists), `detect_secoc_layout` (2.1),
`analyse_reset_behaviour` (exists in `gateway_security.py`).

**Priority**: P1 — these are the most practically exploitable SecOC weaknesses.

### 2.8 SecOC-aware synthetic traffic — P1

**What**: Extend the synthetic traffic generator to produce SecOC-protected
frames with configurable weakness levels (no freshness, short MAC, weak key,
counter that wraps fast) so the detection tools can be tested end-to-end.

**Where**: `core/reverse/synthetic.py` (extend `generate` with SecOC
profiles).

**Depends on**: Existing `synthetic.py` already has `cmac` and
`cmac_no_freshness` behaviours; extend with more SecOC-specific scenarios.

**Priority**: P1 — testing infrastructure.

### 2.9 AUTOSAR SecOC profile classifier — P2

**What**: From the observed MAC length, freshness length and algorithm
(recovered or hinted), classify the SecOC profile:

- Profile 1: CMAC-AES-128, 24-bit truncated MAC, 4-bit freshness.
- Profile 2: CMAC-AES-128, 28-bit MAC, 8-bit freshness.
- Profile 3: CMAC-AES-128, 64-bit MAC, 32-bit freshness (CAN FD).
- Custom / OEM-specific.

**Where**: `core/reverse/crypto.py` (new `classify_secoc_profile`).

**Depends on**: `classify_mac` (exists), `detect_secoc_layout` (2.1).

**Priority**: P2 — informational; helps guide further analysis.

### 2.10 E2E + SecOC interaction analysis — P2

**What**: Some PDUs are protected by both AUTOSAR E2E (CRC + alive counter) and
SecOC (MAC + freshness). Detect when both are present and analyse whether the
E2E counter is independent of the SecOC freshness value. If they share the same
counter, the security margin is lower than it appears (the counter is not an
independent nonce).

**Where**: `core/reverse/crypto.py` (new `analyse_e2e_secoc_interaction`).

**Depends on**: `find_checksums` (exists in `checksums.py`), `detect_secoc_layout` (2.1).

**Priority**: P2 — niche but important for safety-critical PDUs.

---

## 3. Security gateway bypassing (done)

Techniques for analysing and testing gateway filtering, routing rules and
access control. The passive analyses read captured traffic only; the active
tests require `--allow-send` and are for authorised bench testing.

### 3.1 Gateway filter rule inference — P0

**What**: From captured traffic (requests that passed vs. requests that were
blocked), build a model of the gateway's filter rules:

- Per-service allow/deny rules per ECU per session.
- DID-level and routine-level filtering (some gateways filter at the
  sub-function level, not just the service level).
- Payload-length filtering (some gateways reject oversized requests).
- Rate limiting rules (maximum requests per second per ECU).

Output a machine-readable filter ruleset (JSON) that can be used to plan
further testing.

**Where**: `core/reverse/gateway_security.py` (new `infer_filter_rules`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: `access_matrix` (exists), `classify_did_security` (exists).

**Priority**: P0 — the existing access matrix shows allow/deny per service but
does not model the actual filter rules or their granularity.

### 3.2 Functional addressing bypass testing — P1

**What**: Extend the existing functional addressing bypass detection (which is
passive, in `analyse_gateway_filtering`) with an active test:

- Send the same service request via both physical and functional addressing.
- Compare which responses come back.
- Report services that are filtered via physical but pass via functional.

This is an active test (send tier), for authorised benches only.

**Where**: `core/active_security_test.py` (new `functional_bypass_test`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: `analyse_gateway_filtering` (exists, passive detection).

**Priority**: P1 — the passive detector flags potential bypasses; the active
test confirms them.

### 3.3 Session race condition testing — P1

**What**: Test whether the gateway has a race condition between session
transitions and security enforcement:

- Switch to extended session.
- Immediately send a locked service before the gateway's filter update
  propagates.
- Measure the timing window (if any) during which the service succeeds.

The existing `timing_window` detection in `analyse_gateway_filtering` finds
this passively from captured traffic. This feature adds an active,
controlled test.

**Where**: `core/active_security_test.py` (new `session_race_test`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: The existing passive detection in `analyse_gateway_filtering`.

**Priority**: P1 — timing windows are among the most exploitable gateway
weaknesses.

### 3.4 ECU reset bypass testing — P1

**What**: Extend the existing passive `analyse_reset_behaviour` with an active
test:

- Send ECUReset (0x11) to a target ECU.
- Immediately attempt locked services during the boot-up window.
- Measure the window duration and which services are accessible.

**Where**: `core/active_security_test.py` (new `reset_bypass_test`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: `analyse_reset_behaviour` (exists, passive), `replay_test`
(exists, active framework).

**Priority**: P1 — boot-up bypass is a well-known class of gateway weakness.

### 3.5 Gateway message rewriting detection — P1

**What**: Detect whether the gateway rewrites diagnostic messages as they pass
through (e.g. stripping or adding bytes, changing the request ID, translating
sub-functions). Requires captures from both sides of the gateway (two CAN
interfaces).

- Compare request payloads on the external bus with the forwarded payloads on
  the internal bus.
- Report any transformations (ID mapping, payload truncation, byte rewriting).

**Where**: `core/reverse/gateway_security.py` (new `detect_rewriting`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: `map_gateway` (exists). Requires multi-interface capture
support (which exists in the connection manager).

**Priority**: P1 — understanding rewriting is essential for crafting requests
that survive the gateway.

### 3.6 Multi-session unlock chain discovery — P1

**What**: Some gateways require unlocking in a specific order across sessions
(e.g. extended session at level 1, then programming session at level 3, then
back to extended to access the target service). From captured traffic, discover
these multi-step unlock chains.

**Where**: `core/reverse/gateway_security.py` (new `discover_unlock_chains`).

**Depends on**: `extract_unlock_sequence` (exists, single-ECU),
`recover_auth_state_machine` (1.1).

**Priority**: P1 — extends the single-ECU unlock sequence to cross-session
chains.

### 3.7 Non-standard ID range scanning — P1

**What**: Many OEMs use non-standard diagnostic ID ranges (not the ISO 14229
0x700-0x7FF range). Generate a plan to systematically scan ID ranges for
diagnostic responders, including extended IDs. The plan is read-only; execution
uses existing UDS tools.

**Where**: `core/reverse/gateway_security.py` (new `scan_plan_non_standard`).

**Depends on**: `map_gateway` (exists).

**Priority**: P1 — without this, hidden diagnostic endpoints are missed.

### 3.8 Gateway DoS resilience assessment — P2

**What**: Test the gateway's behaviour under high bus load:

- Send a burst of valid but high-rate diagnostic requests.
- Observe whether the gateway drops legitimate traffic, slows down, or
  crashes.
- Measure the maximum sustainable request rate before degradation.

Active test, send tier, authorised benches only.

**Where**: `core/active_security_test.py` (new `dos_resilience_test`),
`mcp/server.py`, `mcp/service.py`.

**Depends on**: `replay_test` (exists, active framework).

**Priority**: P2 — resilience testing is secondary to bypass discovery.

### 3.9 Diagnostic-over-IP (DoIP) gateway analysis — P2

**What**: Extend gateway analysis to DoIP (ISO 13400): detect DoIP gateways
from their activation lines, extract the routing activation and vehicle
identification, and test whether the DoIP gateway enforces the same access
control as the CAN gateway.

**Where**: `core/reverse/gateway_security.py` (new `analyse_doip_gateway`).

**Depends on**: Existing DoIP support (the tool already supports DoIP for UDS
transport). The gateway analysis framework exists; this extends it to the DoIP
routing activation and entity discovery.

**Priority**: P2 — DoIP is growing but most vehicles still use CAN gateways.

### 3.10 Gateway bypass report and remediation guidance — P2

**What**: Consolidate all gateway findings into a structured report with
remediation recommendations:

- For each bypass finding, explain the root cause and suggest a fix.
- Classify findings by UNECE WP.29 R155 / ISO/SAE 21434 categories.
- Output a compliance-oriented summary for OEM reporting.

**Where**: `core/reverse/gateway_security.py` (new
`gateway_compliance_report`).

**Depends on**: `gateway_report` (exists), `infer_filter_rules` (3.1).

**Priority**: P2 — nice-to-have for professional reporting; the raw findings
are already available.

---

## 4. GUI and integration work (done)

### 4.1 Gateway security panel: NRC fingerprint and auth timing views — P0

**What**: The `GatewaySecurityPanel.qml` exists but does not display NRC
fingerprint results, auth timing analysis or the probing plan. Add these
views.

**Where**: `gui/qt/CanExplorer/GatewaySecurityPanel.qml`, `gui/reverse.py`
(expose the new data to QML).

**Depends on**: 1.2 (MCP tools), the core functions exist.

**Priority**: P0 — the data is computed but not visible in the GUI.

### 4.2 SecOC reverse engineering panel — P1

**What**: A new QML panel for SecOC-specific reverse engineering: layout
detection results, freshness reconstruction, MAC strength assessment,
key recovery status.

**Where**: New `gui/qt/CanExplorer/SecOcReversePanel.qml`, `gui/reverse.py`
(new `SecOcReverseTool` class).

**Depends on**: 2.1 (layout detection), 2.2 (freshness reconstruction),
2.3 (strength assessment).

**Priority**: P1 — the crypto panel handles some of this but a dedicated
SecOC panel improves the workflow.

### 4.3 Attack surface dashboard — P2

**What**: A summary dashboard in the GUI showing the combined security posture:
ECU count, bypass count by severity, SecOC coverage, seed quality grades and
top findings. Links to the detail panels.

**Where**: New `gui/qt/CanExplorer/SecurityDashboard.qml`, `gui/reverse.py`.

**Depends on**: 1.7 (attack surface report).

**Priority**: P2 — nice overview but the detail panels already exist.

---

## 5. Test and documentation work (done)

### 5.1 Tests for new core functions — P0

**What**: Every new function in `core/reverse/gateway_security.py` and
`core/reverse/crypto.py` needs unit tests in `tests/unit/core/`. The existing
test files (`test_gateway_security.py`, `test_crypto_analysis.py`) provide the
pattern.

**Where**: `tests/unit/core/`.

**Depends on**: The features they test.

**Priority**: P0 — the project requires all core logic to be unit-tested.

### 5.2 Documentation updates — P0

**What**: Update `docs/user-guide/reverse-engineering.md` and
`docs/user-guide/security-manager.md` with the new capabilities. Add a new
page `docs/user-guide/secoc-reverse-engineering.md` for the SecOC-specific
workflow.

**Where**: `docs/user-guide/`.

**Depends on**: The features they document.

**Priority**: P0 — the project requires docs to stay current
(`mkdocs build --strict`).

### 5.3 Synthetic traffic scenarios for SecOC and gateway testing — P1

**What**: Add synthetic traffic profiles to `core/reverse/synthetic.py` that
generate:

- SecOC-protected frames with configurable weakness levels.
- Gateway traffic with configurable filtering rules and bypass paths.
- Multi-ECU topologies with shared and independent keys.

**Where**: `core/reverse/synthetic.py`, `tests/unit/core/`.

**Depends on**: 2.8 (SecOC synthetic traffic).

**Priority**: P1 — testing infrastructure for end-to-end validation.

---

## Implementation order

Suggested order that maximises early value and respects dependencies:

1. **1.2** Expose existing functions as MCP tools (low effort, unblocks agent
   use of NRC fingerprinting, auth timing and probing planner).
2. **2.1** SecOC layout detection without ARXML (the most requested missing
   capability).
3. **1.1** Auth state machine recovery (builds on existing unlock sequence).
4. **2.2** Freshness reconstruction and sync analysis (builds on 2.1).
5. **2.3** MAC truncation strength assessment (builds on 2.2).
6. **1.3** Multi-capture differential analysis (independent, high value).
7. **3.1** Gateway filter rule inference (builds on existing access matrix).
8. **4.1** GUI views for NRC fingerprint and auth timing (builds on 1.2).
9. **2.7** SecOC bypass detection (builds on 2.1, 2.2).
10. **1.7** Attack surface report (consolidates everything above).
11. Remaining P1 items in any order.
12. P2 items as time allows.

---

## Non-goals

- **Key extraction from hardware**: this tool analyses bus traffic, not chip
  dumps. Hardware attacks (glitching, side-channel on the physical chip) are
  out of scope.
- **Fabricating paywalled specifications**: per the project's honesty policy,
  binary layouts from paywalled standards (RP1210C, parts of J1939-22) are
  labelled but not guessed.
- **Production vehicle use**: all active tests require explicit opt-in flags
  and are designed for authorised bench testing. The tool does not provide
  capabilities for attacking production vehicles.

---

## Permitted

