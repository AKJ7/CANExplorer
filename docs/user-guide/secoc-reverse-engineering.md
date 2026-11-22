# SecOC reverse engineering

The **SecOC reverse engineering** workflow helps you understand and assess the
AUTOSAR Secure Onboard Communication (SecOC) protection on a CAN bus, even
when you do not have an ARXML file describing the PDU layout. All operations
are passive: the tool analyses captured traffic and never transmits on the bus.

## What SecOC is

SecOC adds a **security trailer** to each protected PDU: a truncated
Message Authentication Code (MAC) computed over the authentic payload and a
**freshness value** (counter or timestamp). The receiver verifies the MAC and
rejects frames whose freshness value is outside the expected window, which
prevents both forgery and replay. A typical classic CAN frame looks like this:

```
[ authentic payload ][ freshness counter ][ truncated MAC ]
```

CAN FD frames can carry longer MACs (up to full 128-bit) and wider freshness
values thanks to their larger payload (up to 64 bytes).

## What the tool can detect

From captured traffic alone, the tool can:

- **Detect the SecOC layout** (the boundary between authentic payload,
  freshness value and MAC) without an ARXML, by statistical analysis.
- **Reconstruct the freshness scheme**: counter width, wrap behaviour,
  freshness manager type (monotonic, trip/reset, time-based) and effective
  replay window.
- **Detect synchronisation messages** that reset counters on multiple
  protected PDUs simultaneously.
- **Assess the MAC truncation strength**: forgery probability at the observed
  frame rate, AUTOSAR domain compliance and whether replay beats brute-force
  forgery.
- **Attempt key recovery** using common/weak keys, keys derived from the Data
  ID or ECU serial number.
- **Detect shared keys** across multiple SecOC-protected PDUs.
- **Predict the freshness counter** value at a future time and compute the
  replay window.
- **Detect SecOC bypass paths**: missing trailers, static MACs, counter wrap
  acceptance and post-reset windows.
- **Classify the AUTOSAR SecOC profile** (Profile 1, 2, 3 or custom).
- **Analyse the interaction between E2E and SecOC** when both protections are
  present on the same PDU.

## The SecOC tab in the Reverse engineering view

Open a connection (live or replayed log) and switch to the **Reverse
engineering** view. The **SecOC** tab provides:

### Layout detection

Select an identifier or click **Detect all** to scan every identifier for a
SecOC-like statistical signature. The tool looks for:

- A trailing region whose bytes pass randomness tests (candidate MAC).
- A region immediately before it that increments monotonically (candidate
  freshness counter).

The result shows the detected byte ranges, bit widths and a confidence score.
If you have an ARXML loaded, the detected layout is compared against the
ARXML-defined one.

### Freshness reconstruction

For each detected (or ARXML-defined) SecOC identifier, the tool reconstructs
the freshness scheme:

- **Counter width**: 4, 8, 12, 16 or 24 bits.
- **Wrap count**: how many times the counter wrapped during the capture.
- **Freshness type**: monotonic (increments by 1 per frame), trip/reset
  (resets without reaching max) or time-based (large increments per frame).
- **Effective window**: the time or frame count before the counter wraps and
  old frames become replayable.

### Strength assessment

The strength assessment combines the MAC truncation length, the freshness
window and the observed frame rate to give a practical security rating:

- **Forgery probability** per attempt (2^-MAC_bits).
- **Expected time to forgery** at the observed frame rate.
- **AUTOSAR domain compliance**: whether the truncation length meets the
  recommendations for safety-critical (24+ bits), chassis (28+ bits),
  powertrain (32+ bits) and infotainment (16+ bits) domains.
- **Replay vs. forgery**: whether the counter wraps faster than the expected
  forgery time, making replay + counter collision a viable attack.
- **Severity**: critical, warning, acceptable or strong.

### Key recovery

The tool tries to recover the MAC key by testing common/weak keys against the
observed truncated MACs:

- All-zero, all-one, sequential and other common test keys.
- Keys derived from the Data ID (key = Data ID repeated).
- Keys derived from the ECU serial number (if available).

A recovered key is verified against multiple frames. When multiple identifiers
share a recovered key, the tool reports the shared key group and the
implications.

### Bypass detection

The bypass detector looks for enforcement weaknesses:

- **Missing trailer**: frames shorter than the expected length.
- **Static MAC**: MAC bytes that never change despite different payloads.
- **Counter wrap acceptance**: frames accepted after the counter wraps with no
  visible gap or rejection.
- **Post-reset window**: a period after boot during which SecOC is not yet
  active (early frames show structured MAC bytes, later frames are random).

Each finding includes a severity rating (critical, warning, info) and evidence.

## MCP tools for SecOC analysis

All SecOC analysis functions are exposed as MCP tools, so an AI agent (or any
MCP client) can drive the full workflow programmatically:

| Tool | Purpose |
|---|---|
| `detect_secoc_layout` | Detect the SecOC trailer layout (freshness + MAC boundary) from statistical properties |
| `assess_secoc_strength` | Compute the practical security margin of a SecOC-protected identifier |
| `try_secoc_key_recovery` | Try to recover the SecOC MAC key using common/weak keys |
| `detect_secoc_bypass` | Detect paths where SecOC protection is configured but not enforced |
| `detect_shared_secoc_keys` | Detect whether multiple PDUs share the same MAC key |
| `predict_freshness` | Predict the freshness counter value at a future time |
| `replay_window` | Compute the replay window (time range during which a captured frame can be replayed) |
| `classify_secoc_profile` | Classify the AUTOSAR SecOC profile from observed parameters |
| `analyse_e2e_secoc_interaction` | Analyse whether E2E and SecOC counters on the same PDU are independent |

All tools accept payloads as hex strings and return structured results with
findings and confidence scores.

## Working with and without an ARXML

### With an ARXML

When an ARXML is loaded, the tool knows the exact SecOC configuration for each
PDU: the Data ID, the MAC length, the freshness length, the truncation and the
algorithm. The layout detection step is skipped and the analysis starts directly
from the ARXML-defined parameters.

This is the most reliable mode: the ARXML is the ground truth for the PDU
layout, and the analysis tools only need to assess the *strength* and *enforcement*
of the protection, not discover its structure.

### Without an ARXML

Without an ARXML, the tool uses statistical analysis to discover the SecOC
layout. This is the more common scenario in reverse engineering, where the ARXML
is not available. The workflow starts with layout detection and proceeds through
strength assessment and bypass detection.

The confidence scores on the detected layout indicate how certain the tool is
about the boundary between authentic payload, freshness value and MAC. A
confidence below 0.5 is flagged as uncertain, and the tool suggests inspecting
the bytes manually in the **Bits** tab.

## End-to-end workflow example

This example walks through a complete SecOC reverse engineering session on a
captured log file without an ARXML.

### 1. Open the capture

Open the log file (BLF, ASC, TRC or CSV) via **File > Open log…** or drag it
onto the window.

### 2. Detect SecOC-protected identifiers

In the **Reverse engineering** view, switch to the **SecOC** tab and click
**Detect all**. The tool scans every identifier and reports those with a
SecOC-like statistical signature. For each detected identifier, you see:

- The byte ranges of the authentic payload, freshness counter and MAC.
- The MAC and freshness bit widths.
- A confidence score.

### 3. Assess the freshness scheme

Select a detected identifier. The freshness reconstruction shows the counter
width, wrap behaviour and effective replay window. If you have a long enough
capture, you can see whether the counter wraps and how often.

### 4. Assess the MAC strength

The strength assessment shows the forgery probability, the expected time to
forgery at the observed frame rate and the AUTOSAR domain compliance. If the
counter wraps faster than the expected forgery time, the tool warns that replay
may be easier than forgery.

### 5. Try key recovery

Click **Try key recovery** to test common/weak keys. If a key is found, it is
verified against multiple frames and the confidence score is shown. You can then
check whether other identifiers share the same key.

### 6. Detect bypass paths

The bypass detector scans for enforcement weaknesses. Each finding has a
severity rating and evidence. For example, a "static MAC" finding means the MAC
bytes are always the same regardless of the payload, which suggests the receiver
does not check the MAC.

### 7. Classify the SecOC profile

The profile classifier tells you which AUTOSAR SecOC profile (1, 2, 3 or
custom) matches the observed parameters. This helps guide further analysis and
reporting.

### 8. Export the results

The combined results (layout, freshness, strength, key recovery, bypasses and
profile) can be exported as JSON from the MCP interface or the console.

!!! tip "Using the MCP tools in a script"
    The MCP tools allow you to automate the entire workflow. For example, to
    scan all identifiers, assess their strength and detect bypasses:

    ```python
    # In the CANExplorer console or via MCP
    for identifier in detected_ids:
        layout = detect_secoc_layout(payloads[identifier])
        if layout:
            strength = assess_secoc_strength(
                payloads[identifier],
                timestamps[identifier],
                mac_start=layout.mac_start,
                mac_end=layout.mac_end,
                freshness_start=layout.freshness_start,
                freshness_end=layout.freshness_end,
            )
            bypasses = detect_secoc_bypass(
                payloads[identifier],
                timestamps[identifier],
                mac_start=layout.mac_start,
                mac_end=layout.mac_end,
                freshness_start=layout.freshness_start,
                freshness_end=layout.freshness_end,
            )
    ```
