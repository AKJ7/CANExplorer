# Hardware Security Testing Roadmap

Hardware-level security testing of ECUs on test rigs and benches.
Companion to the [software security reverse engineering
roadmap](security-reverse-engineering.md); features listed there are not
repeated here.

**Scope**: professional automotive security assessments, academic research
and security certification testing (UNECE R155/R156 compliance,
ISO/SAE 21434) performed on ECUs mounted on **lab test rigs and benches**.
Not for production vehicles on roads. Active tests (anything that
transmits or triggers external hardware) are gated behind `--allow-send` /
`--allow-control` and are never enabled by default.

**CANExplorer's role**: CAN-side orchestrator and logger. It coordinates
with external hardware tools (ChipWhisperer, GIAnT, OpenOCD, JTAGulator,
oscilloscopes, logic analysers) via their APIs and serial protocols but
does **not** implement low-level hardware control itself. All glitching,
power analysis, debug probing and voltage manipulation is performed by the
external instrument; CANExplorer provides timing triggers, CAN-side
observation and event correlation.

---

## Existing foundation

The following capabilities already exist and form the base this roadmap
extends:

- **Active security tests** (`core/active_security_test.py`): replay and
  forgery framework with send-tier gating.
- **Gateway security analysis** (`core/reverse/gateway_security.py`):
  topology mapping, auth timing, NRC fingerprinting, reset-behaviour
  analysis.
- **Crypto analysis** (`core/reverse/crypto.py`): randomness tests,
  freshness analysis, MAC classification.
- **SecOC verification** (`core/secoc.py`, `core/secoc_check.py`): MAC
  generation/verification, freshness counter reconstruction.
- **Seed/key cracking** (`core/seedkey_crack.py`): algorithmic seed/key
  recovery.
- **Connection manager** (`gui/connection_manager.py`): multi-interface
  support, bus configuration.

---

## 1. Hardware fault injection coordination

CANExplorer orchestrates fault injection timing and captures the CAN-side
effects. The actual voltage/clock/EM glitching is performed by external
hardware (ChipWhisperer, GIAnT, NewAE NAE-CW, custom glitchers).

### 1.1 External glitcher integration framework -- P0

**What**: A generic integration layer for communicating with external
fault injection hardware. Defines an abstract `GlitcherBackend` interface
with methods for arming, triggering and reading status. Ships with
concrete backends for:

- ChipWhisperer (via `chipwhisperer` Python package): voltage and clock
  glitching.
- GIAnT (via serial protocol): voltage glitching.
- Generic serial trigger: a simple serial-line trigger for custom
  glitchers.

The framework handles trigger-to-CAN timing calibration: it measures the
round-trip latency between issuing a trigger command and observing the
CAN-side effect so that subsequent glitch windows can be positioned
accurately.

**Where**: `core/hardware/glitcher.py` (abstract base + backends),
`core/hardware/__init__.py`.

**Dependencies**: `chipwhisperer` Python package (optional, for CW
backend), `pyserial` (already a dependency). No existing CANExplorer
features required.

**Priority**: P0 -- everything else in this section depends on it.

### 1.2 Voltage glitch campaign runner -- P0

**What**: Orchestrates a voltage glitch campaign: sweeps glitch offset,
width and repeat count while sending a chosen UDS request on the CAN bus
at each parameter combination. For each attempt, records:

- Glitch parameters (offset, width, voltage).
- The UDS request sent and the response received (or timeout).
- Whether the ECU accepted a request it should have rejected (security
  bypass), crashed (no response / ECUReset), or behaved normally.
- Timestamps for correlation with external power traces.

Outputs a structured campaign log (JSON) with per-attempt results and a
summary of successful glitches.

**Where**: `core/hardware/fault_injection.py` (campaign logic),
`mcp/server.py` + `mcp/service.py` (MCP tool `run_glitch_campaign`).

**Dependencies**: 1.1 (glitcher framework), UDS send capability (exists,
send tier).

**Priority**: P0 -- voltage glitching during security access is the most
common hardware attack on ECUs.

### 1.3 Clock glitch coordination -- P1

**What**: Extends the campaign runner with clock-glitch-specific
parameters: glitch cycles, source clock frequency, insertion point
relative to the CAN frame. Uses the ChipWhisperer clock glitch module.

**Where**: `core/hardware/fault_injection.py` (extend campaign runner
with clock glitch mode).

**Dependencies**: 1.1 (glitcher framework, CW backend), 1.2 (campaign
runner).

**Priority**: P1 -- clock glitching is less common than voltage glitching
but effective against certain microcontroller families.

### 1.4 SCA trace capture coordination -- P1

**What**: Coordinates side-channel analysis trace capture with CAN-bus
activity. For each UDS request, CANExplorer:

- Sends the request on the bus.
- Triggers the oscilloscope or ChipWhisperer capture scope to record a
  power/EM trace.
- Reads back the trace and stores it alongside the CAN request/response
  pair.
- Supports averaging over multiple identical requests (for DPA/CPA
  attacks) and varying the input (for known-plaintext correlation).

The traces are stored in a format compatible with Lascar, SCALib and
Jlsca (NumPy `.npy` arrays with a metadata sidecar JSON). CANExplorer
does not perform the actual SCA math (DPA, CPA, template attacks) but
provides the aligned, labelled dataset.

**Where**: `core/hardware/sca_capture.py` (capture coordination),
`mcp/server.py` + `mcp/service.py` (MCP tool
`capture_sca_traces`).

**Dependencies**: 1.1 (glitcher framework for scope integration),
`chipwhisperer` or VISA/SCPI for external scope.

**Priority**: P1 -- SCA requires specialised post-processing tools; our
value is the aligned CAN+trace dataset.

### 1.5 EM fault injection trigger coordination -- P1

**What**: Extends the campaign runner for electromagnetic fault injection
(EMFI). EMFI probes are positioned manually; CANExplorer provides the
electrical trigger signal (via the glitcher backend's trigger output) at
the right moment relative to the CAN request and logs the XY position
(manually entered or read from a motorised stage via serial) alongside
the CAN outcome.

**Where**: `core/hardware/fault_injection.py` (extend with EMFI mode
and XY position logging).

**Dependencies**: 1.1 (glitcher framework), 1.2 (campaign runner).

**Priority**: P1 -- EMFI is increasingly used for secure-boot bypass;
the XY position logging is the unique value CANExplorer adds.

### 1.6 Glitch campaign analysis and visualisation -- P1

**What**: Post-campaign analysis of fault injection results:

- Parameter heatmap: success/fail/crash across the offset x width
  parameter space.
- Timing histogram of ECU response delays under glitching.
- Crash-vs-bypass classification.
- Automatic identification of the most promising parameter window.
- Export to JSON/CSV.

**Where**: `core/hardware/fault_analysis.py` (analysis logic),
`gui/qt/CanExplorer/FaultInjectionPanel.qml` (visualisation),
`gui/hardware.py` (controller).

**Dependencies**: 1.2 (campaign runner, for the log format).

**Priority**: P1 -- without visualisation, parameter sweeps are hard to
interpret.

### 1.7 Automated glitch refinement loop -- P2

**What**: Given a coarse parameter sweep that found at least one
successful glitch, automatically narrows the parameter window using
binary search or Bayesian optimisation to find the minimum-energy
reliable glitch. Runs as a campaign with adaptive parameter selection.

**Where**: `core/hardware/fault_injection.py` (extend campaign runner
with adaptive mode).

**Dependencies**: 1.2 (campaign runner), 1.6 (campaign analysis for
result classification).

**Priority**: P2 -- optimisation refinement; the coarse sweep already
finds the window.

---

## 2. Physical-layer CAN attacks on test rigs

Attacks that operate at the CAN physical layer rather than the
application/diagnostic layer. Require CAN transceivers or interfaces
capable of low-level bus manipulation.

### 2.1 Bus-off attack orchestration -- P0

**What**: Orchestrates a CAN bus-off attack against a target ECU:
transmits frames timed to cause bit-level collisions with the target
ECU's frames, incrementing its transmit error counter until it enters
bus-off state. Requires a CAN interface that supports precise
frame-timing control (e.g. SocketCAN with `TX_DEADLINE`, Vector
hardware, or a dedicated attack interface).

CANExplorer:

- Identifies the target ECU's dominant transmission pattern (ID, period).
- Schedules collision frames at the right offset.
- Monitors the bus for the target's disappearance (bus-off confirmation).
- Logs the error counter progression (if supported by the interface).
- Records the time to bus-off and the number of collisions needed.

**Where**: `core/hardware/phy_attack.py` (bus-off logic),
`mcp/server.py` + `mcp/service.py` (MCP tool `bus_off_attack`).

**Dependencies**: CAN interface with precise timing control. Existing
frame capture and analysis.

**Priority**: P0 -- bus-off is the fundamental CAN physical-layer attack
and a required test for UNECE R155 compliance.

### 2.2 Error frame generation and monitoring -- P0

**What**: Generates and monitors CAN error frames on the test bench:

- **Generation**: sends malformed frames (wrong CRC, wrong DLC, stuff-bit
  violations) to trigger error frames from the target ECU or other nodes.
  Requires an interface capable of raw bit-level transmission (e.g.
  PEAK PCAN with error frame support, or a custom FPGA-based interface).
- **Monitoring**: counts and classifies error frames observed on the bus
  (bit error, stuff error, CRC error, form error, ACK error) using
  interfaces that report error frames (SocketCAN `CAN_ERR_FLAG`).
- Logs error frame rates over time and correlates spikes with specific
  transmitted frames.

**Where**: `core/hardware/phy_attack.py` (generation + monitoring).

**Dependencies**: CAN interface with error frame support. Existing frame
capture.

**Priority**: P0 -- error handling behaviour is a key part of physical-
layer security assessment.

### 2.3 Dominant bit injection -- P1

**What**: Injects a sustained dominant state on the CAN bus for a
configurable duration, disrupting all communication. Monitors which ECUs
recover first (early recovery can indicate safety-critical nodes with
shorter bus-off recovery times). Requires a CAN interface or external
relay that can hold the bus dominant.

CANExplorer logs:

- Injection start and duration.
- Per-ECU recovery time (first valid frame after release).
- Whether any ECU entered a degraded mode (changed frame content or
  rate).

**Where**: `core/hardware/phy_attack.py` (extend with dominant
injection).

**Dependencies**: Interface capable of holding the bus dominant.
Existing frame capture.

**Priority**: P1 -- tests ECU resilience to bus jamming.

### 2.4 CAN transceiver fingerprinting -- P1

**What**: Identifies individual CAN transceivers by their physical-layer
timing characteristics (bit timing, edge slopes, voltage levels).
Requires an oscilloscope or high-speed logic analyser connected to the
CAN-H/CAN-L lines.

CANExplorer:

- Triggers the oscilloscope capture on a specific CAN ID.
- Reads back the waveform (via VISA/SCPI or the scope's Python API).
- Extracts timing features: propagation delay, bit width jitter,
  dominant/recessive voltage levels, edge rise/fall times.
- Clusters frames by physical-layer fingerprint to attribute frames to
  individual transceivers even when IDs are shared.
- Stores fingerprints for cross-session comparison.

**Where**: `core/hardware/phy_fingerprint.py` (fingerprinting logic),
`core/hardware/scope_interface.py` (oscilloscope/logic analyser
integration via VISA/SCPI).

**Dependencies**: Oscilloscope or logic analyser with a programmable
API (VISA/SCPI, Saleae SDK, PicoScope SDK). External `pyvisa` package.

**Priority**: P1 -- transceiver fingerprinting is a growing research
area and useful for identifying rogue or replacement ECUs.

### 2.5 Voltage-level analysis -- P1

**What**: Records and analyses CAN bus voltage levels over time using an
external oscilloscope or ADC:

- CAN-H and CAN-L absolute voltages and differential voltage.
- Common-mode voltage anomalies (can indicate a compromised node).
- Bus termination quality (impedance estimation from waveform ringing).
- Voltage-level drift over temperature or load.

The analysis correlates voltage anomalies with specific CAN frames or
ECU behaviour observed on the logical bus.

**Where**: `core/hardware/phy_fingerprint.py` (extend with voltage
analysis), `core/hardware/scope_interface.py`.

**Dependencies**: 2.4 (scope interface).

**Priority**: P1 -- complements transceiver fingerprinting with bus-level
health assessment.

### 2.6 Physical-layer IDS evasion testing -- P2

**What**: Tests whether a physical-layer intrusion detection system (IDS)
can be evaded by:

- Gradually shifting frame timing to avoid anomaly detection.
- Mimicking the voltage fingerprint of a legitimate ECU.
- Injecting frames during bus-idle periods to avoid collision-based
  detection.

CANExplorer generates the evasion traffic patterns and logs whether the
IDS (a separate system under test) detects or misses them.

**Where**: `core/hardware/phy_attack.py` (extend with evasion
patterns).

**Dependencies**: 2.1 (bus-off), 2.4 (transceiver fingerprinting for
target mimicry). An external physical-layer IDS as the system under test.

**Priority**: P2 -- niche; for teams building or evaluating physical-
layer IDS products.

---

## 3. Debug interface discovery and exploitation assistance

Coordination with external debug tools (JTAGulator, OpenOCD, Bus Pirate)
for discovering and accessing debug interfaces on ECUs under test, with
CAN-side correlation.

### 3.1 JTAG/SWD scan coordination -- P0

**What**: Integrates with JTAGulator (via serial) and OpenOCD (via
Tcl scripting interface) to coordinate JTAG/SWD pin discovery on an ECU
while monitoring CAN-side effects:

- Drives JTAGulator's scan routines and reads back discovered pinouts.
- When a valid JTAG/SWD interface is found, connects via OpenOCD and
  reads the chip ID, debug lock status and memory protection
  configuration.
- Logs CAN bus activity during the scan (some ECUs emit error frames or
  reset when debug pins are probed).
- Records the mapping: ECU identifier (from NRC fingerprint or DID) to
  discovered debug interface pinout and protection status.

**Where**: `core/hardware/debug_interface.py` (JTAGulator + OpenOCD
integration), `mcp/server.py` + `mcp/service.py` (MCP tool
`scan_debug_interface`).

**Dependencies**: JTAGulator (serial), OpenOCD (Tcl socket). Existing
NRC fingerprinting for ECU identification.

**Priority**: P0 -- debug access is often the most direct route to full
ECU compromise; knowing the protection status is essential for assessment
scoping.

### 3.2 UART discovery logging -- P1

**What**: Coordinates UART discovery on ECU test points with CAN-side
observation:

- Drives a Bus Pirate or FTDI adapter through common baud rates on
  candidate pins.
- Captures and stores any output received (boot logs, debug shells,
  error messages).
- Correlates UART output with CAN-bus events: matches ECU boot messages
  on UART with the first CAN frames after power-on, linking the UART
  port to a specific CAN node.

**Where**: `core/hardware/debug_interface.py` (extend with UART
discovery).

**Dependencies**: `pyserial` (already a dependency). Existing frame
capture for CAN-side correlation.

**Priority**: P1 -- UART consoles are common on development ECUs left in
production.

### 3.3 Debug access event correlation -- P1

**What**: A correlation engine that aligns events from multiple sources
in a unified timeline:

- CAN bus frames (with timestamps).
- Debug interface events (OpenOCD memory reads/writes, breakpoint hits).
- UART output lines (with timestamps).
- Glitch trigger events (from the fault injection framework).

Each event is tagged with its source and timestamp. The timeline can be
queried for temporal proximity ("what happened on the CAN bus within
50 ms of this OpenOCD memory write?"). Exports to JSON and CSV.

**Where**: `core/hardware/event_correlator.py`,
`mcp/server.py` + `mcp/service.py` (MCP tool `query_hw_events`).

**Dependencies**: 3.1 (debug interface integration), 1.1 (glitcher
framework for trigger events). CAN frame capture (exists).

**Priority**: P1 -- the correlation timeline is the unique value of
integrating these tools in CANExplorer rather than using them
independently.

### 3.4 Debug protection assessment report -- P1

**What**: Consolidates debug interface findings into a structured report:

- For each ECU: debug interface type (JTAG/SWD/UART), protection status
  (locked/unlocked/partially locked), chip ID, identified
  microcontroller family.
- Cross-references with known debug lock bypass techniques per MCU family
  (from public sources: datasheets, published research papers).
- Classifies the risk level: unlocked debug in production is critical,
  locked but glitch-vulnerable is high, fully locked with RDP level 2
  or equivalent is low.

**Where**: `core/hardware/debug_interface.py` (extend with report
generation).

**Dependencies**: 3.1 (debug scan results). Public MCU datasheet
references only (no paywalled content).

**Priority**: P1 -- the report is the deliverable for a hardware security
assessment.

### 3.5 Memory dump coordination -- P2

**What**: When debug access is obtained (unlocked or bypassed via
glitching), coordinates a full or partial memory dump via OpenOCD while
keeping the CAN bus monitored:

- Reads flash and RAM regions via OpenOCD.
- Monitors CAN bus for watchdog-related resets during the dump (long
  reads can trigger watchdog timeouts).
- Sends UDS TesterPresent (0x3E) or other keep-alive frames if needed to
  prevent ECU timeout during the dump.
- Stores the dump with metadata: source ECU, address range, debug
  interface used, CAN session state at time of dump.

**Where**: `core/hardware/debug_interface.py` (extend with memory dump
coordination).

**Dependencies**: 3.1 (OpenOCD integration). UDS send capability
(exists, for TesterPresent keep-alive).

**Priority**: P2 -- requires an unlocked or bypassed debug interface;
most of the dump work is done by OpenOCD.

---

## 4. SecOC hardware-level testing

Testing SecOC implementations at the hardware level: HSM interaction
timing, side-channel attack coordination for key extraction, fault
injection during MAC computation. Extends the software-side SecOC
analysis in the companion roadmap (sections 2.1-2.10 there).

### 4.1 HSM interaction timing analysis -- P0

**What**: Measures the timing between the ECU receiving a SecOC-protected
frame on the CAN bus and the HSM completing the MAC verification, as
observed through:

- CAN-side: the delay between receiving a SecOC frame and the ECU acting
  on it (e.g. sending a response, changing an output).
- Hardware-side: triggering a scope capture on the SPI/I2C lines between
  the host MCU and the HSM (via the scope interface from 2.4), measuring
  the transaction duration.
- Correlation: aligning CAN timestamps with HSM transaction timing to
  build a timing profile of the SecOC verification path.

This reveals whether the HSM is the bottleneck, whether MAC verification
timing leaks information about the key (constant-time vs. early-exit),
and the ECU's processing budget for SecOC.

**Where**: `core/hardware/secoc_hw.py` (timing analysis),
`core/hardware/scope_interface.py` (scope integration from 2.4).

**Dependencies**: 2.4 (scope interface), existing SecOC frame
identification (`core/reverse/crypto.py`), existing auth timing analysis
(`core/reverse/gateway_security.py`).

**Priority**: P0 -- HSM timing analysis is the entry point for
hardware-level SecOC assessment.

### 4.2 SCA coordination for SecOC key extraction -- P1

**What**: Extends the SCA trace capture framework (1.4) specifically for
SecOC MAC computation:

- Sends known-plaintext SecOC frames at a controlled rate to trigger
  repeated MAC computations on the target ECU's HSM.
- Captures power or EM traces aligned with each MAC computation.
- Labels each trace with the known plaintext, freshness value and
  observed (truncated) MAC.
- Packages the labelled trace set in a format ready for DPA/CPA/template
  attack tools (Lascar, SCALib, ChipWhisperer Analyzer).

CANExplorer does not perform the correlation power analysis itself; it
produces the aligned, labelled dataset that specialised SCA tools
consume.

**Where**: `core/hardware/secoc_hw.py` (extend with SCA campaign for
SecOC), `mcp/server.py` + `mcp/service.py` (MCP tool
`capture_secoc_sca`).

**Dependencies**: 1.4 (SCA trace capture), 1.1 (glitcher framework for
scope triggering). Existing SecOC frame identification. Known-plaintext
requirement: satisfied when ARXML is loaded or the layout detector from
the companion roadmap (2.1 there) has extracted the field boundaries.

**Priority**: P1 -- requires significant external hardware setup but is
the gold-standard attack on HSM-based SecOC.

### 4.3 Fault injection during SecOC MAC computation -- P1

**What**: Times a voltage or clock glitch to hit the ECU during the
narrow window when the HSM is computing or verifying the MAC. The goal
is to bypass the MAC check (the ECU accepts an unauthenticated or
incorrectly authenticated frame):

- Uses the HSM timing profile from 4.1 to position the glitch window.
- Sends a frame with an intentionally wrong MAC while triggering the
  glitch.
- Observes whether the ECU accepts the frame (bypass) or rejects it.
- Sweeps the glitch timing across the MAC verification window.

**Where**: `core/hardware/secoc_hw.py` (extend with fault injection
campaign for SecOC), builds on `core/hardware/fault_injection.py`.

**Dependencies**: 1.2 (glitch campaign runner), 4.1 (HSM timing profile).
Existing SecOC frame construction (`core/secoc.py`).

**Priority**: P1 -- fault injection against SecOC is a key attack
scenario in UNECE R155 threat analysis.

### 4.4 Freshness value manipulation at the physical layer -- P1

**What**: Tests the ECU's handling of manipulated freshness values at
the hardware level:

- Sends SecOC frames with freshness values that are valid but at the
  boundary of the acceptance window (just inside, just outside, wrapped).
- Correlates with the HSM timing to detect whether boundary checking
  takes different time (timing oracle for the acceptance window width).
- Combines with fault injection: glitch during the freshness check
  rather than the MAC check to test whether the two are independently
  protected.

**Where**: `core/hardware/secoc_hw.py` (extend with freshness
manipulation tests).

**Dependencies**: 4.1 (HSM timing), 1.2 (glitch campaign for the
combined test). Existing freshness analysis (`core/reverse/crypto.py`).

**Priority**: P1 -- freshness handling is a separate failure point from
MAC verification.

### 4.5 SecOC fail-open detection under hardware stress -- P2

**What**: Tests whether the ECU falls back to accepting unauthenticated
frames when the HSM is under stress or unavailable:

- Floods the ECU with high-rate SecOC frames to saturate the HSM's
  processing capacity.
- Monitors whether the ECU begins accepting frames without verification
  (fail-open) or starts dropping frames (fail-closed, correct behaviour).
- Combines with power manipulation: briefly drops the HSM's supply
  voltage (via external relay) to simulate HSM failure, then observes
  whether the ECU accepts unauthenticated frames.

**Where**: `core/hardware/secoc_hw.py` (extend with stress and
fail-open tests).

**Dependencies**: 4.1 (HSM timing, to know the saturation rate), 1.1
(glitcher framework for power manipulation), bus-off attack (2.1) for
high-rate frame generation.

**Priority**: P2 -- fail-open behaviour is a critical vulnerability but
the test requires significant hardware setup.

---

## 5. Security gateway hardware testing

Testing gateway ECU behaviour under hardware-level stress and
manipulation. Extends the software gateway analysis in the companion
roadmap (section 3 there) with physical-layer techniques.

### 5.1 Gateway power manipulation during security transitions -- P0

**What**: Tests the gateway's behaviour when power is briefly
interrupted or degraded during a security-sensitive transition:

- During a session change (e.g. default to extended to programming):
  momentarily drops the gateway's supply voltage via an external
  relay/MOSFET controlled by the glitcher backend.
- Observes whether the gateway loses its security state and falls back
  to an unlocked or permissive mode on recovery.
- Logs the full CAN traffic around the power event: frames before, the
  gap during power loss, and the first frames after recovery.
- Sweeps the timing: power glitch at different points during the session
  transition sequence.

**Where**: `core/hardware/gateway_hw.py` (power manipulation + CAN
logging), builds on `core/hardware/fault_injection.py`.

**Dependencies**: 1.1 (glitcher framework for relay control), 1.2
(campaign runner for parameter sweeps). Existing session transition
knowledge from `core/reverse/gateway_security.py`.

**Priority**: P0 -- power glitching during authentication is a standard
test in hardware security assessments of gateways.

### 5.2 Gateway behaviour under bus error conditions -- P0

**What**: Tests how the gateway handles physical-layer errors on its
internal and external bus connections:

- Inject error frames on the external bus (using 2.2) and observe
  whether the gateway continues to forward legitimate frames on the
  internal bus or shuts down.
- Drive the external bus into a high error-rate state and test whether
  the gateway's filtering rules degrade (e.g. starts forwarding
  requests it normally blocks).
- Test bus-off recovery: force the gateway into bus-off on one interface
  and observe whether traffic on the other interface is affected.

Correlates error frame counts with gateway forwarding behaviour using
captures from both sides (requires two CAN interfaces).

**Where**: `core/hardware/gateway_hw.py` (extend with bus error
testing).

**Dependencies**: 2.1 (bus-off attack), 2.2 (error frame generation),
multi-interface capture (exists in connection manager).

**Priority**: P0 -- gateway error handling is a required test for UNECE
R155 compliance.

### 5.3 Multi-bus gateway bypass on test benches -- P1

**What**: Tests whether a gateway can be bypassed by injecting frames
directly on the internal (trusted) bus segment of a test bench:

- With two CAN interfaces, one on each side of the gateway, compares
  which requests pass through the gateway versus which succeed when
  injected directly on the internal bus.
- For each service that the gateway blocks, tests whether the same
  request injected on the internal bus is accepted by the target ECU.
- Maps the gateway's trust boundary: which bus segments are considered
  trusted (no authentication required) versus untrusted.

This is a bench-only test: in a real vehicle, physical access to the
internal bus requires opening the ECU or tapping a harness.

**Where**: `core/hardware/gateway_hw.py` (extend with multi-bus bypass
testing), `mcp/server.py` + `mcp/service.py` (MCP tool
`test_gateway_bypass_hw`).

**Dependencies**: Multi-interface capture (exists). Existing
`access_matrix` and `infer_filter_rules` (companion roadmap 3.1) for
knowing what the gateway blocks.

**Priority**: P1 -- validates the gateway's trust model on the bench.

### 5.4 Gateway clock and supply monitoring -- P1

**What**: Monitors the gateway ECU's supply voltage and clock signal (via
external scope or ADC) during security operations and correlates with
CAN-side behaviour:

- Detects brownouts, voltage sags and clock drift during high bus load.
- Correlates supply anomalies with gateway forwarding delays or dropped
  frames.
- Identifies the minimum supply voltage at which the gateway still
  enforces its security policy.

**Where**: `core/hardware/gateway_hw.py` (extend with supply/clock
monitoring), `core/hardware/scope_interface.py`.

**Dependencies**: 2.4 (scope interface), multi-interface capture
(exists).

**Priority**: P1 -- supply monitoring complements the active power
manipulation tests.

### 5.5 Gateway watchdog and reset behaviour under hardware stress -- P2

**What**: Tests the gateway's watchdog and reset behaviour:

- Triggers repeated rapid power cycles and observes how quickly the
  gateway re-establishes its filtering rules after each reset.
- Measures the window between the gateway's first CAN frame after reset
  and the moment filtering is fully enforced.
- Tests whether the watchdog can be starved by keeping the gateway's CPU
  busy (e.g. flooding with valid but complex diagnostic requests) until
  it resets, and whether the reset creates a bypass window.

**Where**: `core/hardware/gateway_hw.py` (extend with watchdog tests).

**Dependencies**: 5.1 (power manipulation), existing
`analyse_reset_behaviour` for CAN-side window measurement.

**Priority**: P2 -- extends the existing software-side reset-bypass
analysis with hardware-level power cycling.

---

## 6. ECU flash/memory extraction assistance

Coordinating CAN-side UDS flash read with hardware debug access and
logging/correlating events during extraction attempts.

### 6.1 UDS flash read with hardware event logging -- P0

**What**: Coordinates a UDS flash read (ReadMemoryByAddress 0x23 or
RequestUpload 0x35 / TransferData 0x36) with hardware event logging:

- For each transfer block, logs the CAN request/response alongside any
  hardware events (scope captures, debug interface activity, UART
  output).
- Detects and handles common failure modes: ECU reset during transfer
  (restarts from the last successful block), security timeout (re-
  authenticates), and watchdog-triggered interruptions.
- Tracks extraction progress (address ranges read, gaps, retries).
- Outputs the extracted data with a metadata sidecar documenting the
  extraction method, session state and any anomalies.

**Where**: `core/hardware/extraction.py` (extraction coordination),
`mcp/server.py` + `mcp/service.py` (MCP tool `extract_flash`).

**Dependencies**: Existing UDS flash/memory read tools, 3.3 (event
correlator for hardware event logging). Send tier.

**Priority**: P0 -- the most common end-goal of a hardware security
assessment is extracting firmware.

### 6.2 Dual-path extraction (UDS + debug) -- P1

**What**: When both UDS flash read and debug interface access are
available, runs both in parallel and cross-validates the extracted data:

- Reads the same memory region via UDS and via OpenOCD.
- Compares the contents: differences indicate runtime-patched memory,
  encrypted flash read-back, or UDS read protection that returns
  modified data.
- Logs discrepancies with the exact address ranges and byte differences.

**Where**: `core/hardware/extraction.py` (extend with dual-path mode).

**Dependencies**: 6.1 (UDS flash read), 3.1 (OpenOCD debug access).

**Priority**: P1 -- the cross-validation reveals read-back protection
mechanisms that single-path extraction misses.

### 6.3 Flash extraction under fault injection -- P1

**What**: Combines UDS flash read with fault injection to bypass read
protection:

- Sends a ReadMemoryByAddress request for a protected region.
- Times a glitch to hit during the access-control check.
- If the ECU returns data instead of NRC 0x33 (security access denied),
  stores the extracted block and continues.
- Sweeps glitch timing across the access-control window, using the
  response NRC as the oracle.

**Where**: `core/hardware/extraction.py` (extend with fault-injection
extraction mode), builds on `core/hardware/fault_injection.py`.

**Dependencies**: 1.2 (glitch campaign runner), 6.1 (flash read
coordination). Existing UDS tools.

**Priority**: P1 -- the combination of UDS flash read + glitching is a
well-established attack on locked ECUs.

### 6.4 Extraction progress dashboard -- P2

**What**: A GUI panel showing flash extraction progress:

- Memory map with coloured regions: extracted (green), in-progress
  (yellow), failed (red), not attempted (grey).
- Per-block status: address, size, method (UDS/debug/glitch), retries,
  timestamp.
- Live update during extraction.

**Where**: `gui/qt/CanExplorer/ExtractionPanel.qml` (visualisation),
`gui/hardware.py` (controller).

**Dependencies**: 6.1 (extraction coordination for the data model).

**Priority**: P2 -- useful for long extraction sessions but not
essential for the core workflow.

---

## 7. Infrastructure and integration

### 7.1 Hardware configuration and connection manager -- P0

**What**: Extends the existing connection manager with hardware tool
configuration:

- A "Hardware Tools" section for configuring glitcher backends,
  oscilloscope connections, debug tool connections and relay controls.
- Connection health monitoring: periodic ping/status check of each
  connected hardware tool.
- Configuration persistence: save and load hardware bench setups
  (JSON), so a configured test rig can be recalled between sessions.

**Where**: `gui/qt/CanExplorer/HardwareConnectionDialog.qml` (UI),
`gui/hardware.py` (controller), `core/hardware/config.py`
(configuration model).

**Dependencies**: 1.1 (glitcher framework), existing connection manager
pattern.

**Priority**: P0 -- the entry point for all hardware testing workflows.

### 7.2 Hardware safety interlocks -- P0

**What**: Software safety interlocks for hardware testing:

- **Voltage limits**: the glitch campaign runner refuses to set
  parameters outside the configured safe range for the target MCU's
  supply voltage.
- **Timeout**: all hardware operations have a configurable timeout; if
  the ECU does not respond within the timeout, the glitcher is
  disarmed and the bus is released.
- **Emergency stop**: a single command (`--stop-all` / MCP tool
  `emergency_stop`) that immediately disarms all glitchers, releases
  all bus-dominant states and closes all debug connections.
- **Confirmation prompts**: destructive operations (bus-off, power
  manipulation, flash overwrite) require explicit confirmation unless
  `--batch` mode is set.

**Where**: `core/hardware/safety.py` (interlock logic),
`mcp/server.py` (emergency stop MCP tool).

**Dependencies**: 1.1 (glitcher framework), 2.1 (bus-off).

**Priority**: P0 -- safety interlocks must be in place before any
hardware testing feature ships.

### 7.3 Unified event timeline -- P1

**What**: A GUI panel showing the unified event timeline from the event
correlator (3.3):

- Zoomable timeline with colour-coded lanes: CAN, debug, UART, glitch,
  scope.
- Click-to-inspect: click an event to see its full details.
- Filter by source, event type, ECU or time range.
- Export the visible window to JSON/CSV.

**Where**: `gui/qt/CanExplorer/HardwareTimelinePanel.qml`
(visualisation), `gui/hardware.py` (controller).

**Dependencies**: 3.3 (event correlator).

**Priority**: P1 -- the timeline is the primary tool for understanding
hardware+CAN interactions.

### 7.4 Bench configuration templates -- P2

**What**: Pre-built configuration templates for common test bench
setups:

- ChipWhisperer + SocketCAN: voltage glitching with CAN monitoring.
- OpenOCD + Vector: debug access with professional CAN interface.
- PicoScope + PEAK PCAN: physical-layer analysis with CAN monitoring.
- Full setup: all of the above.

Each template pre-fills the hardware connection dialog with the right
interface types, connection parameters and safety limits.

**Where**: `core/hardware/config.py` (extend with templates),
`gui/qt/CanExplorer/HardwareConnectionDialog.qml`.

**Dependencies**: 7.1 (hardware connection manager).

**Priority**: P2 -- convenience; the manual configuration already works.

---

## Implementation order

Suggested order that maximises early value and respects dependencies:

1. **1.1** Glitcher integration framework (unblocks all fault injection
   work).
2. **7.2** Safety interlocks (must exist before any hardware operations
   ship).
3. **7.1** Hardware connection manager (entry point for configuring the
   bench).
4. **1.2** Voltage glitch campaign runner (the highest-value hardware
   attack).
5. **2.1** Bus-off attack orchestration (fundamental physical-layer
   attack).
6. **2.2** Error frame generation and monitoring (complements bus-off).
7. **3.1** JTAG/SWD scan coordination (debug access discovery).
8. **4.1** HSM interaction timing analysis (entry point for SecOC
   hardware testing).
9. **5.1** Gateway power manipulation (gateway hardware testing).
10. **5.2** Gateway under bus error conditions (builds on 2.1, 2.2).
11. **6.1** UDS flash read with hardware event logging (extraction
    coordination).
12. **3.3** Debug access event correlation (unified timeline).
13. **1.4** SCA trace capture coordination (builds on 1.1).
14. **1.6** Glitch campaign analysis and visualisation (GUI for results).
15. Remaining P1 items in dependency order.
16. P2 items as time allows.

---

## Non-goals

- **Implementing low-level hardware control**: CANExplorer sends trigger
  commands and reads results from external tools. It does not bit-bang
  JTAG, generate glitch waveforms, or drive oscilloscope ADCs directly.
  The hardware does the hardware work.
- **Production vehicle testing**: all features in this roadmap assume
  ECUs on a bench/test rig in a controlled lab environment with explicit
  authorisation. No feature is designed for use on vehicles in traffic.
- **SCA/DPA computation**: CANExplorer captures and labels traces; the
  statistical attack (correlation power analysis, template attacks) is
  performed by dedicated tools (Lascar, SCALib, ChipWhisperer Analyzer).
- **Hardware design**: CANExplorer does not design or manufacture test
  fixtures, probes or adapters. It assumes the user has the physical
  setup in place.
- **Fabricating paywalled specifications**: per the project's honesty
  policy, binary layouts from paywalled standards are labelled but not
  guessed.
- **Features already in the software roadmap**: software-side SecOC
  reverse engineering, gateway filtering inference, seed/key cracking
  algorithms, UDS service enumeration and all other features listed in
  `security-reverse-engineering.md` are not repeated here. This roadmap
  covers only the hardware-integration extensions.

---

## External hardware compatibility targets

The following external tools are the primary integration targets,
chosen because they have documented APIs and are widely used in
automotive security testing:

| Category | Tool | Interface | Python package |
|---|---|---|---|
| Glitcher | ChipWhisperer (Husky/Pro) | USB | `chipwhisperer` |
| Glitcher | GIAnT | Serial | custom serial protocol |
| Scope | PicoScope | USB | `picosdk` / `ctypes` |
| Scope | Keysight/Rigol/Siglent | VISA/SCPI | `pyvisa` |
| Logic analyser | Saleae Logic | USB | Saleae automation API |
| Debug | OpenOCD | Tcl socket | `socket` (stdlib) |
| Debug | JTAGulator | Serial | `pyserial` |
| Debug | Bus Pirate | Serial | `pyserial` |
| Relay/switch | USB relay boards | Serial/HID | `pyserial` / `hidapi` |
