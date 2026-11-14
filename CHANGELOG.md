# Changelog

All notable changes to CANExplorer are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

CANExplorer follows [semantic versioning](https://semver.org/) from 1.0.0 on. Between `0.0.1` and `1.0.0`, the
development stages are listed by date and commit, newest first.

## [Unreleased]

## [1.8.0] – 2026-11-14

### Added

- **Camera video and GPS map in step with recordings** (*Video & map* view). Pick a camera in the Record dialog
  to record a video next to the log (`drive.blf` → `drive.mp4`), in H.264 where the graphics hardware encodes it,
  else in MPEG-4 Part 2 (switched to automatically when the hardware encoder fails); a `drive.blf.media.json` sidecar ties the
  interface's clock to the video, also for split and triggered recordings. Replaying the log plays the video at
  the replay's position and speed, through pausing and seeking. GPX tracks and NMEA 0183 logs (RMC/GGA) can be
  attached to a replay, aligned by UTC time (or at the start, for tracks without dates), and are shown on a map
  with the position, speed and altitude at the replay time. Video and GPS offsets fine-tune the alignment.
  Map tiles (OpenStreetMap by default, any `{z}/{x}/{y}` server) are off until enabled in the preferences, and
  are cached on disk.
- **Trace time formats.** A selector in the trace toolbar (and the multi-bus trace) shows frame times as
  relative (seconds since the start), delta (since the previous row, following the display filter), absolute
  (local time of day) or Unix time. The choice is shared by all traces and remembered.
- **Security Manager** (menu and command palette): a password-protected key store for the keys and certificates
  of secured vehicle networks, and the tools that use them. Secrets are encrypted (scrypt, AES-256-GCM, each
  entry's settings bound into its encryption); only names and settings are shown while it is locked, and keys
  never reach logs, reports or the MCP interface. `CANEXPLORER_KEYSTORE_PASSWORD` unlocks it for headless runs.
  - **SecOC checks in the trace.** The secured PDUs of an ARXML (`SECURED-I-PDU`) are verified frame by frame:
    `[SecOC ok, FV 1042]`, `MAC ERROR`, or `FRESHNESS ERROR` for a replayed value. Bit-packed trailers of any
    length are read; CMAC/AES and HMAC-SHA-256 are recognised from the ARXML's algorithm names (others can be set
    on the key, never guessed). Freshness values are reconstructed as counters from their transmitted low bits,
    or by a manufacturer scheme given as a Python file. The toolbar shows *SecOC: n of m checked*. Scripts can
    build secured PDUs with `SecOcVerifier.secure`.
  - **DoIP over TLS with certificates**: verify the entity against trusted certificates and present a client
    certificate (mutual TLS); the top bar says when the entity was verified. `SSLKEYLOGFILE` writes the session
    keys for decrypting captures.
  - **IPsec and MACsec decryption in Ethernet captures**: ESP with AES-GCM, AES-CBC or NULL encryption (with
    HMAC-SHA-256/SHA-1), tunnel and transport mode, and MACsec GCM-AES-128/256, encrypted or integrity-only. The
    packet list shows what the packets carried, and the details pane a dissection of the decrypted frame.
  - **Sending secured PDUs**: the restbus simulation, the Transmit panel (**SecOC**, on by default) and test
    scripts secure SecOC secured PDUs with a new freshness value and MAC in every frame. Periodic sending prepares
    each frame afresh (software timing).
  - **SecOC statistics and test steps**: counts of frames verified, MAC errors, freshness errors and frames sent per
    secured PDU in the connection toolbar, the MCP tool `secoc_status` (results only, never keys), and the test
    steps `t.secoc_status`, `t.expect_secoc_ok` and `t.send(..., secure=…)`.
  - **SecOC security tests**: `t.send_secoc_attack` sends frames an ECU must reject — a replay, a stale freshness
    value, a wrong MAC, a flipped payload bit — so test benches can check that it does.
  - **TLS in Ethernet captures**: **TLS key log…** decrypts TLS 1.2 and 1.3 sessions (AES-GCM, ChaCha20-Poly1305)
    with an `SSLKEYLOGFILE` key log and decodes what they carried, e.g. DoIP; TLS packets are recognised as such.
  - **Seed/key algorithms in the key store**: UDS seed/key scripts are kept encrypted and chosen with **From key
    store** in the Diagnostics (and Flashing) and GMLAN views.
  - **Certificate expiry notices** for TLS certificates that expired or expire within 30 days.
- **Security Manager, continued:**
  - **Key bundles**: export chosen entries to a file encrypted with its own passphrase, and import them on another
    computer (existing entries are kept unless replaced).
  - **Remember the password in the OS keychain** (Windows Credential Manager, macOS Keychain, Secret Service), so
    the key store unlocks at start; off by default. New dependency: `keyring`.
  - **Test certificates**: a CA with a tester and an ECU certificate (EC, Ed25519 or RSA) for benches and
    simulators, ready for DoIP over TLS and UDS Authentication.
  - **AUTOSAR trip/reset freshness values** for SecOC: trip, reset and message counters synchronised by a
    TripResetSyncMsg, received and sent as in Annex A of the AUTOSAR SecOC specification (§11.4).
  - **Security events** page per connection: SecOC and E2E failures, unknown identifiers, frames faster than their
    cycle time and refused security access or authentication, counted per identifier with severity, exportable as
    CSV or JSON.
- **Security testing and evidence:**
  - **Security access tests** (Diagnostics, test scripts): seed analysis (repeated seeds, constant bytes,
    predictable steps, short seeds; seeds renewed by session change, ECU reset or repeated requests) and a lockout
    test (attempts until exceedNumberOfAttempts, the delay, and whether an ECU reset ends it). The simulated ECU
    can now generate random seeds and lock out wrong keys.
  - **Audit log** of the security manager: unlocking, entries added, exported and imported, keys and
    certificates used, authentications, security tests and attack frames, each record chained with an HMAC under
    a key in the key store; **Verify** shows whether records were changed, removed or reordered.
  - **Security events in Ethernet captures**: SOME/IP E2E failures, IPsec/MACsec integrity failures, TLS records
    that fail to decrypt, refused DoIP routing activations and security access, SOME/IP services not offered.
  - **Certificate revocation lists** in the key store, applied to DoIP TLS and UDS Authentication (a revoked
    certificate is refused; the simulated ECU answers NRC 0x57).
  - **SecOC and E2E checks of the PDUs inside container frames**, in the trace and the security events.
  - **MKA (MACsec Key Agreement, EAPOL) decoded** in Ethernet captures: key server, SCI, message number and
    parameter sets.
- **UDS Authentication (0x29) by certificate exchange**: the Diagnostics view (also over DoIP) authenticates with
  a client certificate from the Security Manager — verifyCertificateUnidirectional or Bidirectional, then
  proofOfOwnership signed over the ECU's challenge (ECDSA, Ed25519 or RSA, as the ECU expects) — checks a
  bidirectional ECU's certificate and proof, and de-authenticates. Test scripts use `t.uds.authenticate(…)`. All
  sub-functions and the authentication NRCs (0x34, 0x50–0x5D) are named in the trace. The message layouts follow
  python-udsoncan (cross-checked byte for byte) and the exchange the public AUTOSAR Dcm specification. The
  simulated ECU supports it, protecting DIDs with `authenticationRequired`.
- **UDS Authentication by challenge-response** (requestChallengeForAuthentication, then
  verifyProofOfOwnershipUnidirectional or Bidirectional, 0x05–0x07): authentication without certificates, with a
  key shared with the ECU (new *UDS authentication key* entries: HMAC-SHA-256 or AES-CMAC over the challenge), a
  client certificate's private key (a signature) or a security profile; the algorithmIndicator as hex or an OID,
  additional parameters, and a bidirectional ECU's proof checked. **Configuration** in the Authentication section
  asks the ECU which method it offers. What the proof covers is manufacturer specific, so the built-in proofs are
  documented as a choice and profiles can supply their own. Test scripts use
  `t.uds.authenticate_challenge_response(…)`; the simulated ECU supports it too.
- **Security profiles and key servers** in the Security Manager: manufacturer plug-ins (a Python file, or an
  installed package's entry point) supplying SecOC keys or authenticators, freshness values, seed/key, and
  certificates and signatures for UDS Authentication, so secrets need not be kept on this computer. **Key server**
  entries reach a server over HTTPS with a bearer token (a documented REST API): seed/key and signatures are
  computed by the server, SecOC keys held in memory only. **From Security Manager** in the Diagnostics and GMLAN
  views chooses a profile's seed/key; profile certificates appear in the Authentication section.
- **E2E checks of SOME/IP messages.** E2E protections are configured per SOME/IP method or event (**E2E…** in the
  SOME/IP tool, **SOME/IP E2E…** in the Ethernet capture) and checked as messages arrive: the CRC, the length and
  Data ID of the E2E header, and the counter sequence (`REPEATED`, `WRONG SEQUENCE`, or ok with lost messages
  counted). The protected data starts at the SOME/IP Request ID, as AUTOSAR specifies. The protections are kept
  between sessions and can be exported and imported as JSON. They are not read from ARXML yet.
- **E2E profiles 4, 6 and 7** (CRC-32P4, CRC-16 and CRC-64 with an explicit length), the profiles for SOME/IP and
  other large data, with a configurable header offset; verified against the protocol examples of the AUTOSAR E2E
  specification, with and without the SOME/IP header. The CRC-64 routine is new too.
- **K-Line interface (ISO 9141-2 / ISO 14230 KWP2000).** A new python-can backend (`elm327_kline`) drives an
  ELM327/OBDLink adapter on the 10.4 kbit/s K-Line diagnostic bus of non-CAN OBD-II vehicles (roughly 1996–2008,
  many European and Asian cars). Each K-Line frame is presented as a frame with the three header bytes (format,
  target, source) in the extended identifier and the payload as the data, so the trace, dashboard, reverse
  engineering and recording work on it. The channel is the adapter's serial port (as for `elm327`) with an optional
  `@3`/`@4`/`@5` suffix choosing ISO 9141-2 slow init, ISO 14230 slow init or ISO 14230 fast init (the default).
  K-Line is request/response, so frames usually appear only once a tester has started a session. A new **K-Line**
  trace decoder names the KWP2000 and OBD-II/J1979 service payloads (which carry no ISO-TP, unlike the KWP2000
  decoder).
- **J1708 interface.** A new python-can backend (`j1708`) captures a live SAE J1708 bus — the 9600-baud two-wire
  (RS-485) serial bus of older heavy trucks — through a USB RS-485/J1708 adapter, so it can be monitored, recorded
  and decoded. Each J1708 message (the MID, its data and checksum) is a frame whose identifier is the MID; select
  the existing **J1587** decoder to describe the parameters. The channel is the serial port with an optional
  `@<baud>` suffix (default 9600). Messages are framed by the idle gaps between them (so back-to-back messages with
  no detectable gap can merge), and sending is best-effort (a plain UART cannot reproduce J1708 bus arbitration);
  both limits are documented.
- **Undo and redo in the database editor** (↶ ↷, ++ctrl+z++ / ++ctrl+shift+z++, ++ctrl+y++ on Windows), for CAN
  and LIN databases: every edit — fields, adding, removing, duplicating, copying and pasting messages and signals,
  removing nodes, promoting reverse-engineering results (one step each) — can be undone, and undo selects what it
  changed. Undoing back to the saved content clears the unsaved-changes mark, and the history survives saving. An
  edit that fails no longer leaves a partial change behind. Steps copy only the messages they change, so undo stays
  fast on large databases.
- **Duplicate and copy in the database editor**: duplicate a message with its signals (next free identifier),
  duplicate a signal, or copy a signal and paste it into another message, placed at the first free bits where it
  fits.
- **Search signals in the database editor**: the search field also finds messages by the names of their signals,
  listing the matching signals under each message.
- **Signals by sender** in the database editor: the database as a collapsible tree of sending nodes, their messages
  and their signals (LIN: by publisher), filtered by the same search, with the matches unfolded. Nodes start
  unfolded and messages folded; ⊞ and ⊟ unfold and fold everything.
- **Live AUTOSAR E2E checks from ARXML.** When an ARXML system description protects PDUs with E2E (an
  `END-TO-END-PROTECTION-SET`), the trace checks the CRC of every received frame of those PDUs and shows the result
  in its description, e.g. `[E2E P05 ok, counter 7]` or `[E2E P05 CRC ERROR 0x0D05, expected 0x0D04, counter 7]`.
  Profiles 2, 5 and 22 are checked; the toolbar shows *E2E: n of m checked*. A protection whose profile is not
  implemented, cannot be told from its category, or whose Data IDs do not fit is shown as not checked with the
  reason (the profile is never guessed). Counter gaps are not tracked, and PDUs inside container frames are not
  checked.
- **Copy System Report** (menu and command palette) copies what a bug report needs: the CANExplorer, Python, PyQt,
  Qt and python-can versions, the operating system and desktop, what each CAN driver reported in the last channel
  detection (it runs one first if needed), and whether the last crash left a crash report.
- **Channel detection says why a device is missing.** **Detect** now records, per driver, the channels it found or
  why it found none — its driver library is not installed or does not load (with the error), it cannot list
  channels, it raised an error, or it timed out — in the log and the system report. Drivers that fail while all
  are probed at once are probed again one at a time (some vendor libraries do not tolerate being loaded next to
  other drivers). The MCP `interfaces` tool no longer returns nothing when a single driver fails.
- **A bug report template** on GitHub asking for the system report and the crash report.
- **System theme**: follows the light or dark mode of the operating system and switches along with it. **In light
  mode** and **In dark mode** choose the theme it shows in each (by default Light and Dark), e.g. Solarized Light
  and Solarized Dark. Offered first in the theme list.
- **States read by shape, not only colour**: the marks after the connections in the sidebar — a filled circle when
  open, a hollow ring when closed, a circle with "!" after an error — and the gateway rule marks (filled circle,
  diamond, ring) can be told apart by colour-blind users and in the monochrome Night Vision theme; hovering a
  connection or a rule names its state. The tools use the same marks — running (a database open in the database
  editor, the multi-bus trace merging, EtherCAT, Physical I/O, DoIP, SOME/IP, Ethernet capture, gateway, a
  comparison), set up but idle (the multi-bus trace paused or waiting for its connections), a warning (unsaved database changes, gateway rules waiting for
  their connections, Ethernet security events) or an error (SOME/IP or capture errors) — and so do the views of a
  connection: a mark after a view while something runs in it (tester present, OBD-II live
  values, flashing, a UDS discovery scan, XCP, the LIN schedule, a simulation, the restbus, tests, camera recording),
  or when it has a warning or an error to look at (bus error states, security events, a failed test run, flash or
  scan); hovering the view says which.
- **Theme previews**: each theme in **Settings → Theme** shows swatches of its colours, and picking one previews it
  on the whole window at once; **OK** keeps it, **Cancel** goes back to the previous theme.
- **Thirteen new themes** (44 in all):
  - light partners of dark themes: **Solarized Light** and **Nord Light**;
  - popular editor palettes: **Catppuccin Latte** and **Catppuccin Mocha**, **Gruvbox Dark** and **Gruvbox Light**,
    **Tokyo Night** and **One Dark**;
  - colour-blind-safe themes on the Okabe-Ito colours: **Colour-Safe Dark** and **Colour-Safe Light** at normal
    contrast, and **High Contrast Light** (black on white) for bright workshops and laptops in sunlight;
  - **Night Vision**, dim reds on black for working in a vehicle at night without losing dark adaptation;
  - **Phosphor**, a green oscilloscope screen.

  Each palette follows its published colours, made lighter or darker where needed to meet the contrast minimums.
- **Secured frames in the automatic analysis** (reverse engineering, also over MCP). With an ARXML describing
  secured PDUs, their SecOC freshness value and truncated MAC are labelled at the ARXML's bits (*SecOC freshness*,
  *SecOC MAC*) and only the rest is analysed. Without one, random-looking trailing bytes — every bit changing in,
  and set in, about half of 100 or more frames — are labelled *Random (MAC?)*, as a hint, instead of being split into
  meaningless values; a checksum at the end of a frame stays a checksum.
- **UDS discovery unlocks a security level before scanning** (**Unlock level**), with the key algorithm chosen in
  the Diagnostics view (a file, a Security Manager seed/key algorithm or a security profile), so DIDs, routines and
  services that answer *securityAccessDenied* can be scanned too. If the unlock fails, nothing is probed.

### Changed

- **`cryptography` is a dependency**: the SecOC CMAC now uses it instead of a pure-Python AES (same results,
  checked against the NIST and FIPS test vectors). The DoIP simulator can serve TLS (`DoipEntitySimulator(tls=…)`).
- **The Diagnostics view is reorganised.** The services are grouped into titled sections (Target ECU, Session,
  Data identifiers, Security access, Routine control, Raw request) in a resizable left column; the trouble codes
  (with their Read/Clear buttons and status mask) and the request history are stacked on the right. The header shows
  the transport, connection, tester-present and loaded-description state. DTCs show their status bits inline and are
  marked *Confirmed* or *Pending*. History entries are numbered cards with the service name and an outcome mark that
  tells positive responses, negative responses and timeouts apart by shape. Errors appear in a dismissible banner,
  and both lists show a hint while empty.
- **The GMLAN view is reorganised like the Diagnostics view.** The services are grouped into titled sections
  (Target ECU, Diagnostic mode, Data identifiers, Security access, Raw request) in a resizable left column; the
  trouble codes and the request history are stacked on the right, as numbered cards with an outcome mark. The header
  shows the single-wire bus and what the page has done to the ECU — the diagnostic operation initiated, normal
  communication disabled, the security level unlocked and tester present — until *Return to normal* or a change of
  ECU addressing. Tester present is a switch, the security level a hex spin box, and errors appear in a dismissible
  banner.
- **The About dialog shows the build's commit and its real build date.** Bundles record the commit, the build date
  (UTC) and the packaging backend when they are built; from a source tree the commit is read from git. "Built on"
  showed the current date instead of the build date. The crash report and the system report show the same.
- **Every theme meets the WCAG contrast minimums**: 4.5:1 for text and secondary text, 3:1 for the accent, status
  and chart colours, against every panel. Colours that fell short were made lighter or darker, keeping their hue —
  most visibly the secondary text of Dracula and Solarized Dark, the status and chart colours of the light themes
  (Beach, Beach Towels, Pastel Rainbow, Aquarius, Pink Gold, Bumblebee, Light) and Rainbow's indigo, which was
  nearly invisible on its background. Bumblebee's fourth chart colour is now blue (it matched the second).
- **Chart colours that looked alike were replaced** with clearly different ones, so overlaid signals stay
  distinguishable: two cyans and two purples in Purple, two greens in Forest, two teals in Moonlight, two seafoams in
  Lollipop, green and teal in Pastel Rainbow, and two reds in Pink Gold.
- **Text on accent colours is white or black, whichever is more legible**, instead of always white: on filled
  buttons, check boxes, selected text, badges, the play button and the dashboard icons. White text failed on most
  themes' accents (e.g. 1.5:1 on Cyberpunk's cyan); among others the default Dark theme's filled buttons now have
  black text.

### Fixed

- **The Security events page no longer leaves a wide gap at the top while empty.** Without events, the
  toolbar and the explanation were spread down the page; they now sit under the title.
- **The traffic breakdown donut no longer overruns with very small shares.** A segment below about 0.6 % (e.g. a
  few extended frames among many standard ones) was drawn the long way round, over the whole chart. Small segments
  now keep a thin slice of their own, empty ones are left out, and the legend shows shares that round to zero as
  "<0.1%" instead of "0.0%".
- **Auto-scroll keeps following the trace after rows are selected.** It used to stop for as long as any row was
  selected, and could stop short of the newest frame when many arrived at once. It now follows new frames whenever
  it is ticked; jumping to a search result or a bookmark unticks it so that frame stays in view, and ticking it again
  jumps to the newest frame. Following new frames scrolls the table smoothly instead of re-laying it out on every
  refresh.
- **Clicking rows in the trace and the overview selects them reliably.** A left click no longer clears the
  selection, a right click selects the row it is on (keeping an existing selection that includes it, so the context
  menu acts on all selected rows), ++shift++ + click selects the range from the current row, ++ctrl++ + click adds
  or removes a row, and ++ctrl+shift++ + click adds a range. Rows are selected on press, so a click also counts when
  the pointer moves slightly or the button is held. The overview now has a context menu too (copy the selected
  rows, select all).
- **An empty drop-down list now shows "No items"** when opened, instead of a thin, half-drawn strip under the box.
- **Main window failing to load without a QtMultimedia backend.** On Linux systems without PulseAudio
  (`libpulse.so.0`), QtMultimedia could not load, so the whole main window failed. Now only the *Video & map*
  view is unavailable there. The `.deb` package now depends on `libpulse0`.
- **TLS key log path on Windows.** The Ethernet view showed the loaded key log path with forward slashes; it now
  uses the platform's native separators.
- **Simulated DoIP entity failing to start on Windows** ("An attempt was made to access a socket in a way
  forbidden by its access permissions"). Windows reserves blocks of ports for UDP, and the free ports tried one
  after another could all lie in one such block; the retries now pick ports at random, also when TCP refuses one.
- **Channel detection retries overrunning their time on Windows.** After a backend's retry used up the time left,
  the coarse Windows clock could still show time remaining, so the next failed backend was retried anyway. A
  retry that raised also ended the whole detection instead of being reported as that backend's error.
- **Freeze when opening the *Video & map* view on Linux without a sound server.** Qt Multimedia logs a message
  while it loads, which could deadlock the application when QML loaded it in the background; it is now loaded at
  start-up. The `.deb` package also depends on `libxrandr2`, needed by Qt's FFmpeg media backend.

## [1.7.1] – 2026-11-13

### Added

- **Crash reports.** When CANExplorer crashes (e.g. a segmentation fault inside Qt), it now leaves a report with
  the Python stack of every thread and a header with the CANExplorer, Python, PyQt and Qt versions, the operating
  system and the Qt platform and theme. At the next start a warning names the file (`crash.previous.log` in
  `~/.local/state/CANExplorer` on Linux, `%LOCALAPPDATA%\CANExplorer` on Windows, `~/Library/Logs/CANExplorer` on
  macOS) to attach to a bug report. The cause of an exception that makes PyQt abort is recorded too. A run that
  exits normally leaves no report.
- **The About dialog confirms the copy.** **Copy to Clipboard** now shows *Copied to clipboard* next to the button
  for a moment.

### Fixed

- **Opening a file dialog no longer crashes the application on Linux desktops without a native file dialog**
  ([#17](https://github.com/AKJ7/CANExplorer/issues/17)). On a bare Wayland compositor or X11 window manager, Qt
  Quick's file dialog fell back to a QML implementation that cannot load from the PyQt6 wheels (they do not ship
  the `Qt.labs.folderlistmodel` module it needs), and Qt then crashed with a segmentation fault — for example on
  **Key file…** in Diagnostics, but equally on every other open or save dialog. All file dialogs are now shown with
  QtWidgets' `QFileDialog`: the desktop's own dialog where there is one, Qt's built-in dialog otherwise. Save
  dialogs still add the chosen format's extension when none is typed.
- **The simulated DoIP entity starts reliably on Windows.** It listened on a free TCP port and then bound UDP to
  the same number, which Windows can refuse (*WinError 10013*) when the port lies in a range reserved for UDP;
  it now tries other free ports until both bind. A failed start no longer leaves the TCP port open.

## [1.7.0] – 2026-11-08

### Added

- **Fade-highlight mode in the Overview.** Tick **Fade changes** and each changed byte glows in the highlight
  colour and cools back to the text colour over about a second, so you can watch which bytes are active while you
  operate the vehicle (rather than only seeing which bytes differ from the one previous frame).
- **Cross-platform live capture** in the Ethernet capture tool. Live capture now works on **Linux, macOS and
  Windows** through libpcap (Npcap on Windows), via a dependency-free ctypes wrapper (`core/pcap_live.py`); on Linux
  it falls back to the previous `AF_PACKET` socket when libpcap is absent. The interface list comes from libpcap
  where available. (Capture still needs privileges — root/`CAP_NET_RAW`, BPF access or Npcap.)
- **AVTP stream health** in the Ethernet capture tool: an **AVTP streams** card listing each stream (by stream id
  and format) with its packet count, **lost frames** (inferred from gaps in the 8-bit AVTP sequence number), loss %
  and average rate. Sequence wrap-around, duplicates and reordered frames are accounted for separately.
- **More E2E profiles.** `can_explorer.core.e2e` now implements AUTOSAR E2E **Profile 5** (CRC16, 8-bit counter,
  implicit 16-bit Data ID) and **Profile 22** (Profile-2 layout with the CRC8H2F polynomial), alongside Profile 2 —
  each verified against the worked examples in the AUTOSAR E2E Protocol Specification.
- **Per-packet field dissection** in the Ethernet capture tool: click a packet to open a details pane with its
  field-by-field breakdown layer by layer — Ethernet, VLAN/R-TAG, IPv4/IPv6, UDP/TCP and the recognised application
  protocol (SOME/IP messages, AVTP-ACF tunnelled CAN frames), each row recording the byte span it covers.
- **AVTP stream formats decoded** in the Ethernet capture tool: **AAF** (audio — format, sample rate, channels, bit
  depth), **CVF** (compressed video — format and codec: MJPEG/H.264/H.265) and **CRF** (clock reference — type, base
  frequency, timestamp interval) header fields, following the open COVESA Open1722 version-0 layouts. The media
  payload itself is not decoded.
- **AUTOSAR UDP-NM decoding** in the Ethernet capture tool: set the **UDP-NM port** (UDP-NM has no standard port) and
  matching frames are decoded as **UDP-NM**, showing the source node, control-bit-vector flags and requested partial
  networks; the capture is re-decoded when the port changes.
- **Fuller SOME/IP payload deserialization**: **optional (TLV) members** (tag/length/value — members may be absent,
  out of order, or unknown and skipped), **unions** and **bit fields**. Unions are read from FIBEX and ARXML and TLV
  data IDs from FIBEX; the decoder itself also supports bit fields and ARXML TLV for hand-built descriptions.
- **Broader automotive-Ethernet decoding** in the Ethernet capture tool: the **SRP/TSN** stream-reservation control
  protocols (MVRP, MSRP, MMRP) and the 802.1CB **R-TAG** (FRER) redundancy tag; **MACsec** (802.1AE) and **IPsec**
  ESP/AH; **IGMP** and **MLD** multicast membership; and the infrastructure protocols **DHCP** and **DNS/mDNS**
  (with the queried name). AVTP **MAAP** address-allocation messages are named with their probe/defend/announce
  type.

- **Restbus simulation.** A new **Restbus** view on every connection that can transmit sends the connection's
  database messages periodically at their cycle times, so an ECU on the bench sees the rest of the bus. Choose which
  messages to send (cyclic ones are on by default, with All / None / Cyclic-only), optionally exclude the node you
  are testing, set a default cycle for event messages, and edit each message's signal values (even while running).
  The view follows the connection's database as it is loaded or changed. Message counters and E2E/CRC checksums are
  not computed (documented in the view's page).
- **Clicking the CANExplorer logo/title in the sidebar returns to the welcome page**, from any connection or tool.
- **Keyboard shortcuts to switch between open connections** — **Ctrl+Tab** / **Ctrl+Shift+Tab** (or
  **Ctrl+PageDown** / **Ctrl+PageUp**) cycle to the next or previous connection, wrapping around, like browser tabs.
- **Editing a read-only database is no longer a dead end.** ARXML can be read but not written; the database editor
  now points **Save** and **Save as…** at the Save-as dialog with a writable file name suggested (the same file as
  a `.dbc`), so every database that can be edited can always be saved in a writable format (DBC, DBF, KCD or SYM).
- **In-app help.** The user guide is now available inside the app — press **F1** or the **?** button in the top
  bar for the page that matches the current view, or open **Help** from the menu. The help panel has a table of
  contents for the user guide, full-text search, back/forward navigation and a link to the online docs, and scales
  screenshots to fit the panel. It is served from the very same Markdown as the
  [documentation website](https://akj7.github.io/CANExplorer/) (bundled with the app), so the two never diverge.
- **The sidebar can now be collapsed to an icon rail** — the logo and the connection/view/tool icons stay visible
  (labels hidden, names shown as tooltips), giving the views more width without losing navigation. Toggle it with
  the button at the left of the top bar or with **Ctrl+\\**; the state is remembered between sessions.
- **Fieldbus tunnelled over automotive Ethernet (IEEE 1722 ACF)** — the Ethernet capture tool decodes the frames
  carried in AVTP Acyclic Control Format messages (TSCF and NTSCF carriers): **CAN / CAN FD** (`ACF_CAN`,
  `ACF_CAN_BRIEF`, labelled **ACF-CAN**), **LIN** (`ACF_LIN`, **ACF-LIN**) and **FlexRay** (`ACF_FlexRay`,
  **ACF-FlexRay**), summarised with the bus id and each frame's identifier/data; other ACF types are named but not
  expanded. **Export CAN log…** saves the tunnelled CAN frames to a CAN log file (`.asc`, `.blf`, `.log`, …) so they
  can be replayed through the ordinary CAN views, plots and reverse-engineering tools. The byte layouts follow the
  open COVESA Open1722 implementation of IEEE 1722-2016, cross-checked against AUTOSAR; only what those open sources
  pin down is expanded (see the Ethernet tool's documented limits).
- **KWP2000 decoder** — a new connection/`decode_protocol` protocol for Keyword Protocol 2000 (ISO 14230), the
  diagnostic protocol that preceded UDS (vehicles from roughly 1996–2006): the same ISO-TP transport and
  request/response convention as UDS, with the older ISO 14230-3 service set and response codes.
- **J1939-22 (J1939-FD) decoder** — a new protocol for J1939 on CAN FD: single-PG frames decode like J1939 on the
  longer FD payload, and the FD transport (FD.TP) and multiplexed-PG (Multi-PG) frames are recognised and labelled.
  (Expanding a Multi-PG frame into its contained PGs and reassembling FD.TP need the byte layout from the paywalled
  SAE J1939-22 standard, so those frames are labelled but not yet expanded.)
- **Network Management (NM) decoder** — a new connection/`decode_protocol` protocol that decodes AUTOSAR NM PDUs
  (the format shared by CAN-NM and UDP-NM): the source node id, the control-bit flags (Repeat Message, Active
  Wakeup, Sleep Ready, Partial-Network Learning) and the requested partial-network clusters. Useful for seeing who
  keeps a bus awake and which partial networks are requested.
- **Secure Onboard Communication (SecOC)** — a new protocol that splits a secured PDU into the authentic payload,
  the truncated freshness value and the truncated MAC, plus a library (`can_explorer.core.secoc`) with a
  self-contained **CMAC/AES-128** (verified against the NIST SP 800-38B and FIPS-197 test vectors) and the SecOC
  generate/verify operations — the authentication counterpart to the E2E (CRC) library.
- **RP1210 adapter support** — a new `rp1210` interface for TMC RP1210 heavy-duty (truck, bus, off-highway)
  adapters: the Nexiq USB-Link, DG Technologies DPA 5, Noregon DLA+, Cummins INLINE and others. It enumerates the
  installed drivers from `RP121032.INI` (and each vendor's INI) and opens their CAN protocol. RP1210 drivers are
  32-bit Windows DLLs, so CANExplorer must run as 32-bit Python to use them; it says so if it cannot load one.
- **ELM327 over Bluetooth without binding a serial port** — an ELM327/OBDLink channel may now be a Bluetooth
  address, `bt://AA:BB:CC:DD:EE:FF` (optionally `…/<channel>`), and CANExplorer opens the RFCOMM connection itself
  instead of needing `rfcomm bind` first. Linux only; on Windows and macOS use the adapter's serial port as before.
- **Many more colour themes**, selectable in the preferences. First **Bumblebee** (a bright honey-yellow light
  theme with black-and-amber accents), **Pink Gold** (a soft rose-pink light theme with gold accents) and
  **Amethyst** (a vivid deep-violet background with a full-spectrum chart palette). A theme is now treated as light
  or dark by its background's luminance, so light-background themes get the light-mode adjustments automatically.
  Then fifteen more adapted from a collection of beautiful colour palettes: the dark **Beautiful Blues**,
  **Blueberry**, **Midnight Plum**, **Purple Skyline**, **Moonlight**, **Lollipop**, **Space Gray**, **Cappuccino**,
  **Ember**, **Metro** and **Prism**, and the light **Beach Towels**, **Beach**, **Pastel Rainbow** and
  **Aquarius**. Each theme's chart palette carries six distinguishable hues so overlaid signals stay legible even
  when the source palette is near-monochrome. Finally **Rainbow**, whose chart palette is the seven canonical
  rainbow colours (ROYGBIV) in order on a deep neutral base. The theme picker now lists all themes alphabetically.

### Changed

- **Code formatting and linting now use [Ruff](https://docs.astral.sh/ruff/) alone** (`ruff format`) instead of
  Black; the style is unchanged (single quotes, 120-column lines). Contributors need only Ruff, and CI checks the
  formatting with `ruff format --check`.
- **The automatic reverse-engineering analysis is roughly 3–4× faster.** The bit analysis now works on the
  payloads as a NumPy matrix: the per-bit change counts (the bulk of the multiplexer search and of the field split),
  and the per-frame CRC/XOR/sum checksum search, are computed in one vectorised pass instead of walking the bits of
  every frame in Python. The results are unchanged. NumPy is now a core dependency.
- **More of the reverse-engineering and plotting work is vectorised with NumPy, with unchanged results.** Signal
  plots reduce each series to one min/max per pixel column in a single pass (much faster redraws on long captures);
  reference correlation samples and scores every candidate field as arrays (about 10× faster); and the
  stimulus–response diff counts bit transitions in one pass. (The bit-transition raster was already optimal and is
  unchanged.)

### Fixed

- **The database editor no longer freezes on large databases.** Editing (adding, removing or changing a message
  or signal) revalidated *every* message in the database on each edit; it now revalidates only the message that
  changed. Searching, and every edit, rebuilt the whole message and issue lists in several places (search did it on
  every keystroke); the lists are now built once per change and reused, and the search filters the cached list. On
  a database of 500+ messages, searching, adding, removing and saving are now responsive instead of pausing for a
  couple of seconds.
- **Corrected two AVTP (IEEE 1722) subtype labels** in the Ethernet capture tool: subtype `0x82` is **NTSCF**
  (it was mislabelled "ADP") and `0x6E` is **AEF (continuous)** (it was mislabelled "NTSCF"), matching the IEEE
  1722 subtype assignments.
- **Starting a connection's in-app MCP server no longer fails with "Unable to configure formatter 'default'" on
  Windows.** In a windowed build there is no console, so `sys.stdout` is `None` and uvicorn's default logging
  configuration — a colour formatter that probes `sys.stdout` — could not be built, which surfaced as that error
  when choosing an MCP (agent) permission tier. The in-app server now starts uvicorn with its own logging
  configuration disabled and relies on the application's logging instead.

## [1.6.0] – 2026-10-31

### Added

- **Automotive Ethernet capture** tool: open a **pcap** or **pcapng** file (or capture live on Linux) and see the
  decoded packets — Ethernet, 802.1Q VLAN, IPv4/IPv6, ARP, UDP/TCP — with the automotive protocols on top
  recognised: **SOME/IP** (and SOME/IP-SD), **DoIP**, **gPTP** (IEEE 802.1AS), **AVTP** (IEEE 1722, with the stream
  id and sequence number) and **LLDP** (neighbour chassis/port, system name, TTL). SOME/IP carried over **TCP**
  (reassembled across segments) and **SOME/IP-TP** segmented messages are decoded in full; a **SOME/IP services**
  card lists the services offered in Service Discovery (ids, version, endpoints, offer count), and when the capture
  contains gPTP a **time-sync** card summarises the grandmaster and its clock quality, the clocks and domains seen,
  and the configured and observed Sync rate. Filter by protocol, address or port, see the protocol breakdown, and
  export back to pcap. Live capture needs root/`CAP_NET_RAW` and a tap or mirror port for a full view.

- **Much larger MCP surface.** The `can-mcp` server gains **diagnostics** tools — OBD-II (`obd_status`, `obd_dtcs`,
  `obd_vin`, `obd_supported`, `obd_read`, `obd_clear`) and UDS (`uds_read_did`, `uds_read_dtcs`, `uds_reset`,
  `uds_routine`, `uds_request`) — **protocol decoding** (`decode_protocol`), more **reverse-engineering** verbs
  (`timing`, `related_signals`, `capture_reaction`), **transmit by name** (`send_message`), and `list_interfaces`.
  The bus is also exposed as read-only **resources** (`can://status`, `can://identifiers`, `can://frames`,
  `can://database`) and guided **prompts** (reverse-engineer an identifier, diagnose trouble codes, find a signal).
  New **permission tiers** gate them: `--allow-diagnostics` (safe reads), `--allow-control` (active diagnostics) and
  `--allow-send` (transmit), each granting the ones below it. `--interface`/`--channel` are now optional — the
  server can start unconnected and the agent opens a bus itself with the `connect` tool (after `list_interfaces`),
  and can switch buses with `disconnect`. A database can be loaded or cleared at runtime (`load_database` /
  `unload_database`).
- **In-app MCP server.** The GUI can host the MCP server for a connection it already has open (the **Agent…**
  button on the Trace toolbar), so an agent attaches to the same live bus you are watching — which a separate
  `can-mcp` process usually cannot, as most adapters are single-access. It is off by default, listens on
  `127.0.0.1` only over the streamable-HTTP transport, is gated by a generated token, and uses the same permission
  tiers (read-only by default).
- **OBD-II page** (SAE J1979 over CAN): a dedicated page for the standard emissions diagnostics. Scan a vehicle
  for its VIN, MIL status and supported values; watch the live values (engine speed, vehicle speed, temperatures,
  load, throttle, fuel level, voltage…) as tiles; and read the stored and pending trouble codes (decoded to
  `P`/`C`/`B`/`U` codes) or clear them. Built on the UDS/ISO-TP stack; an `ObdServer` answers for use without a
  vehicle.
- **NMEA 2000 and J1587 trace decoders.** NMEA 2000 (the marine protocol, built on J1939) decodes the common PGNs
  into fields — engine, position, COG/SOG, heading, wind, depth, speed, battery — in marine units, with Fast Packet
  reassembly. J1587 decodes the older heavy-truck bus's messages (MID and PIDs), with common parameters scaled;
  J1708 is a serial bus (not CAN), so the decoder works on J1587 message bytes from a log or gateway, not a live
  capture. Both are selectable as the connection protocol, like J1939 and CANopen.
- **Bit-transition activity map** in the reverse engineering view (the **Activity** tab): a raster of every bit of
  the selected identifier across the whole capture — frames across, bits down (byte 0 at the top) — so counters
  (diagonal ramps), constant bits (flat), toggles and multiplexed regions stand out at a glance, with changed bits
  highlighted. Complements the frame-at-a-time temporal replay.
- **Interactive scripting console** in the reverse engineering view (the **Console** tab): run Python over the
  frames captured so far, with the reverse-engineering primitives (`collect`, `analyse`, `BitField`, `candidates`,
  `signal_groups`, `correlate`) and the current frames, found fields and helpers in scope, with Python syntax
  highlighting. Variables persist between runs and a trailing expression's value is shown, like a REPL — the
  scriptable surface the MCP service gives an AI agent, for a human at the keyboard.
- **Frame-interval (Δt) and per-byte timing graphs** in the reverse engineering view: **Plot Δt** graphs a
  selected identifier's inter-frame interval over time (on the Signals page, with cursors, statistics and CSV
  export) to spot gateways, bursts and event-triggered messages, and **Plot bytes** graphs every payload byte as
  a value over time.
- **GM Class 2 / SAE J1850 VPW** support, for the pre-CAN GM vehicles such as the 1999–2006 P01/P59 PCMs. A new
  python-can interface (**elm327_vpw**) drives an ELM327/OBDLink adapter in J1850 VPW mode (`AT SP 2`), so the
  10.4 kbit/s single-wire Class 2 bus appears in the connection dialog and the whole application — trace,
  dashboard, reverse engineering, recording — works on it, with each VPW frame's
  three-byte header (priority, target, source) in the extended identifier. A **J1850 / Class 2** diagnostics page
  (shown on these connections) runs SAE J1979 over the shared adapter: read current-data PIDs and the VIN, read
  the stored trouble codes (`P0143`…) or clear them, and send raw requests. Monitoring and diagnostics are
  time-multiplexed on the one adapter. Scripts use `can_explorer.core.j1850` (`J1850Client`) and
  `can_explorer.core.elm327_vpw`; a `Class2EcuSimulator` allows trying it without hardware.
- **ARXML SOME/IP descriptions**: the SOME/IP tool reads AUTOSAR **ARXML** service interfaces (chosen by the
  `.arxml` extension) in addition to FIBEX, resolving the `SOMEIP-SERVICE-INTERFACE-DEPLOYMENT` service/method/event
  ids to the referenced interfaces and their data types to decode payloads into named signals. The same subset as
  FIBEX is supported — scalars, fixed and dynamic arrays, strings and nested structures.
- **AUTOSAR E2E protection** library (`can_explorer.core.e2e`): the AUTOSAR CRC routines (`crc8`, `crc8h2f`,
  `crc16`, `crc32`, `crc32p4`, verified against the published check values) and E2E Profile 2 protect/check, for
  verifying a SOME/IP or signal PDU's end-to-end CRC and sequence counter.
- **German, French, Japanese and Chinese translations** for the newer views (Ethernet capture, SOME/IP, OBD-II,
  J1850, the AI agent/MCP dialog and the reverse-engineering panels), which were previously untranslated.

### Changed

- **`mcp` is now a core dependency**, not an optional extra: the `can-mcp` command and the in-app **Agent** server
  work out of the box (no `pip install 'can-explorer[mcp]'`).
- **More reverse-engineering over MCP.** New tools let an agent inspect raw bit behaviour and test and commit its
  own hypotheses: `bit_activity` (per-bit set/flip rates and a per-byte summary), `extract_field` (decode a
  candidate bit range over time without a database), `mark`/`clear_markers`/`reaction_at_markers` (find the fields
  that react at the moments of a repeated action); `timing` gains a `series` option (the Δt over time) and
  `promote_to_dbc` can now write field specs the agent supplies (`fields=[…]`), not only auto-found fields.
- **Download buttons on the documentation home page**: per-platform buttons (Windows, macOS, Linux AppImage and
  .deb) that link straight to the release's installers, resolved from the GitHub release at page load and with the
  visitor's platform highlighted; they fall back to the releases page without JavaScript.
- **Expanded documentation**: the MCP page leads with the in-app server and a screenshot, plus runnable Python
  scripting examples for both the in-app server and the `can-mcp` command; the OBD-II page gains a screenshot and an
  `ObdClient` scripting example.

### Fixed

- **UDS discovery** scans now stop immediately when **Stop** is pressed, and the button returns to **Start**.
  Stopping interrupts the in-flight request instead of only taking effect between requests (so a scan of an
  unresponsive bus, or an ECU looping on "response pending", no longer appears to ignore Stop until the per-request
  timeout elapses), and the running state is updated once the scan task has actually finished.
- **Closing a connection** no longer leaves the remaining connection's view squeezed into a narrow column: the
  connection views are no longer hosted in a `StackLayout`, which did not reliably resize its current view when a
  connection was added or removed.
- **Closing a connection** no longer empties the sidebar's view list: when a connection was removed, the current
  view binding kept pointing at the destroyed delegate, so the per-connection views (Dashboard, Trace, …)
  disappeared from the sidebar for the connection that remained; it now re-resolves when connections change.
- **UDS/OBD requests now skip stale responses** left on a shared response id (for example by a live poll that was
  just stopped), rather than mistaking the first unrelated response for the answer — a response is accepted only if
  it matches the request's service. This fixes an intermittent "Unexpected response …" failure when reading trouble
  codes right after toggling live data off.
- **Reverse-engineering frame replay steps by frame, not by time**, so stepping and scrubbing are exact even when a
  coarse clock gives several frames the same timestamp (seen on Windows); playback still runs at the recorded pace.
- **Example engine simulation**: the coolant temperature now cools down when the engine is off or idling instead of
  only ever warming up — it tracks a load-dependent operating temperature and falls toward ambient when off.

## [1.5.0] – 2026-10-30

### Added

- **SOME/IP over Ethernet** tool: listens on UDP, discovers the services offered through SOME/IP-SD (Service
  Discovery) — with their instances, versions and endpoints — and shows the decoded messages (requests, responses,
  errors with return codes, events, and the SD entries OfferService / FindService / SubscribeEventgroup). Load a
  **FIBEX** file to decode the message payloads into named signals (scalars, strings, fixed and dynamic arrays and
  nested structs, with a method's request and response decoded separately). Capture is over UDP.
- **Capture reaction** in the reverse engineering view: record a steady baseline, perform an action, and diff the
  two to find what changed on the bus — identifiers that appeared and runs of bits that were stable during the
  baseline but changed during the action (with their before/after values), most reactive first. It is passive
  (it only reads captured frames, never transmits), so it finds which message controls a relay, light or door
  without sending unknown frames; replay the captured frame to confirm. A one-click generalization of markers.
- **Five more colour themes**, selectable in the preferences: **Dracula**, **Solarized Dark**, **High Contrast**
  (a colour-blind-safe, high-contrast theme using the Okabe-Ito palette on black), **Cyberpunk** (neon on
  near-black) and **Rosé** (hot pink on a deep rosy background). Eleven themes in all.

### Fixed

- **J2534 devices**: a device whose USB and Bluetooth connections register as two entries sharing one driver DLL
  (such as the Autel MaxiFlash VCI) now shows both; detection no longer dropped the second one when
  deduplicating the 64/32-bit registry views by library path.

## [1.4.0] – 2026-10-29

### Added

- **Temporal replay** in the reverse engineering Bits tab: **Replay** plays the selected identifier's captured
  frames back over time with a scrubber, play/pause, speed and frame stepping. While replaying, the bit grid, byte
  values and field value show the payload at the scrub position, with bits that just changed lighting up and
  fading — so you can scrub to any moment and watch the bits flow and flip.
- **MCP interface** (`can-mcp`, the `mcp` extra): exposes a CAN bus to an AI agent over the Model Context
  Protocol (stdio). Read-only by default — `bus_status`, `list_identifiers`, `get_frames` (decoded with a
  database), `decode_frame`, `analyze_identifier` (the automatic reverse-engineering analysis), `correlate`
  (rank fields against a reference signal, CSV column or inline series), `promote_to_dbc` (write found fields to a
  DBC) and `load_database` — with `send_frame` and `query_obd` added only when started with `--allow-send`. The
  logic is a Qt-free, SDK-free service layer wrapping a `BusSession`, so it works with any python-can interface,
  including a remote bus shared by `can-server`.

### Changed

- The replay playback bar's 10 s skip buttons use the themed back/forward glyphs (◀◀ / ▶▶) instead of coloured
  emoji, to match the rest of the controls.

### Fixed

- **GVRET devices** (ESP32RET and other SavvyCAN-protocol boards): CANExplorer now sends a keepalive once a second,
  as SavvyCAN does. Some firmware stops streaming frames when the host goes quiet, so capturing could stop after
  some minutes. The receive queue is also bounded now, so a stalled consumer drops frames (counted and logged)
  instead of the buffer growing without limit.

## [1.3.0] – 2026-10-27

### Added

- **GMLAN single-wire CAN (SWCAN)**: connections can open a GM single-wire low-speed bus at 33.3 kbit/s — tick
  **Single-wire CAN (GMLAN)** in the new-connection dialog. It sets the bitrate and selects the single-wire
  transceiver on interfaces that have one (e.g. Kvaser); on SocketCAN and the virtual interface it is the 33.3
  kbit/s bitrate. The frames are ordinary CAN frames, so the whole application works on a single-wire bus.
- **GMLAN diagnostics** (GMW3110): a **GMLAN** page on each CAN connection speaking the GM/Opel diagnostic
  protocol over ISO-TP — one-byte data identifiers (ReadDataByIdentifier 0x1A / WriteDataByIdentifier 0x3B),
  InitiateDiagnosticOperation, DisableNormalCommunication, ReturnToNormalMode, ReportProgrammedState, security
  access, ReadDiagnosticInformation (DTCs as `P0123-04`) and ClearDiagnosticInformation, TesterPresent keep-alive
  and a raw-request field. A simulated GMLAN ECU (`can_explorer.core.simulation.GmlanEcuSimulator`) allows trying
  it without hardware; scripts use `can_explorer.core.gmlan` (`GmlanClient`).
- **French, Chinese and Japanese translations** of the whole interface, selectable under **Settings → Language**
  (or following the system language). Every view is covered — the dashboard, trace, signals, bus health, reverse
  engineering, the database editor, diagnostics, flashing, UDS/XCP, DoIP, J1939, CANopen, EtherCAT, the bus
  gateway, physical I/O, simulation and tests — along with the menus, dialogs and settings. The existing German
  translation was completed to the same coverage. Texts without a translation fall back to English.

- **XCP/CCP measurement & calibration**: a page on each CAN connection that connects to an ECU's XCP (ASAM MCD-1
  XCP) or CCP slave and reads measurements live and reads or writes calibration values — by memory address, or by
  name in physical units from a loaded **A2L** file (ASAM MCD-2 MC: measurements and scalar characteristics with
  IDENTICAL/LINEAR/RAT_FUNC conversions and the standard data types). Simulated XCP and CCP slaves
  (`can_explorer.core.xcp`, `can_explorer.core.ccp`) and `examples/calibration/example.a2l` allow trying it and
  testing without hardware; scripts use `XcpClient`/`CcpClient` and `can_explorer.core.a2l`.

- **UDS fuzzing**: a *Fuzz* mode on the UDS discovery page sends mutated requests (bit flips, boundary bytes,
  truncation, extension) from seed requests or random ones, and flags the anomalies: a positive response to a
  malformed request, an unusual rejection, and — via a periodic TesterPresent liveness check — the ECU going
  unresponsive (a possible hang or crash), on which the run stops. Reproducible from a seed number. Needs the
  state-changing opt-in. Scripts use `can_explorer.core.uds_fuzz.fuzz`.
- **Related signals** (reverse engineering): groups fields that carry the same signal across messages — the same
  quantity re-broadcast by a gateway, or a scaled or lagged copy — by turning each value field into a reference
  and correlating the others with it. Each group gives every member's factor, offset, lag and correlation to the
  group's reference, to label unknowns from an already-identified one. Scripts use
  `can_explorer.core.reverse.related.signal_groups`.
- **Promote to database** (reverse engineering): a button on the automatic analysis panel that turns every found
  field (except constant bits) into a signal in the database editor in one step — creating each message as needed
  and recording each field's kind, multiplexer and details as the signal comment — ready to review and save as a
  DBC. Where interpretations of the same bits compete, the most confident one wins, so the result has no
  overlapping signals. Scripts use `AnalysisController.found_signals` with
  `DatabaseEditorController.add_found_signals`.
- **Multiplexer editing** in the database editor: a signal's **Multiplexing** can be set to *Multiplexer* (the
  switch) or *Multiplexed* with its switch values (e.g. `0, 1, 3`); when a message has several multiplexers, which
  switch a signal follows is chosen too. The switch cannot be dropped while signals follow it, and renaming it
  keeps them pointing at it. Scripts use `DatabaseEditor.update_signal` with `is_multiplexer`, `multiplexer_ids`
  and `multiplexer_signal`.

### Changed

- New application logo: a diamond mark used for the window, desktop entry, installers and documentation (rendered
  from `docs/assets/logo.svg` by `tools/make_icons.py`). In the interface — the sidebar, welcome page and about
  dialog — it is drawn in the current theme's accent colour, so the logo follows the selected theme.
- The startup update check now runs every time CANExplorer starts (a few seconds after the window opens), instead
  of at most once a day, so a new release is noticed on the next launch. **Preferences → Updates → Check at start**
  still turns it off.

### Fixed

- The *update available* dialog no longer clips its content (release notes, hint and buttons) in a short window:
  it is capped to the window height and the release-notes area scrolls when there is not enough room.
- The *unsaved database changes* and *discard changes* dialogs no longer run their message off the edge of the
  dialog: the width is capped and the message wraps, so a long database name stays inside the dialog.

## [1.2.0] – 2026-10-21

### Added

- **Reverse engineering** view of a connection, for finding signals without a database: a bit grid per identifier
  showing how often each bit changes and flashing changed bits; a **Notch** that hides the bits changed so far,
  so the bits of an action (pressing a button, opening a door) stand out, with the identifiers having such bits
  and those appearing only after the notch; per byte the distinct values, range, entropy and a histogram.
- **Bit fields**: select bits in the grid (Intel or Motorola, signed, factor, offset, unit) and **Plot** them on
  the Signals page, from the first frame in the trace on, without a database; plotted fields are saved in
  workspaces. **Add to database** adds the field as a signal, and its message if needed, in the database editor.
- **Automatic analysis** of the reverse engineering view: finds the fields of every identifier and their kind:
  checksums (CRC-8 variants with any start value or data ID, AUTOSAR E2E profile 2 style data IDs, XOR and sums),
  alive counters, multiplexers (with the fields per multiplexer value), physical values (Intel or Motorola,
  signed when they cross zero), states, flags and constant bits.
- **Reference signals**: ranks the fields of the trace by their correlation with a database signal, a column of a
  CSV file (e.g. a GPS log) or OBD-II values polled while recording (engine speed, vehicle speed, temperatures,
  …), with the factor, offset and time lag of the best match.
- **Markers**: mark the moments an action happens (++ctrl+m++); the fields changing around most markers but
  rarely otherwise are listed with their values before and after.
- Multiplexed bit fields can be plotted (taking only the frames with their multiplexer value) and are saved in
  workspaces.
- `tools/synthetic_traffic.py` writes log files with synthetic traffic from a signal database (smooth, constant,
  counting, toggling, noisy signals, CRC checksums and steps at given times), for trying the reverse engineering
  tools; the tests use it to check them against known signals.
- **ODX/PDX diagnostic descriptions**: ECU descriptions in the ASAM MCD-2 D / ISO 22901 ODX format (`.odx`,
  `.odx-d`) and packaged ODX archives (`.pdx`) are read alongside CANdelaStudio CDD, to name data identifiers and
  decode ReadDataByIdentifier responses. Fixed-length data object properties with IDENTICAL, LINEAR or TEXTTABLE
  compu methods are decoded (unsigned, signed, float, ASCII and UTF-8, in both byte orders); other data is shown
  in hex. Load one with **Description…** in the Diagnostics view or with **Load database…**.
- **DoIP tool** (Diagnostics over IP, ISO 13400-2): UDS diagnostics and flashing of vehicles with an Ethernet
  diagnostic port. **Discover** sends a vehicle identification request and lists the DoIP entities answering
  (address, logical address, VIN, EID); **Connect** opens the TCP connection with routing activation (tester
  address, activation type, protocol version 2012 or 2019, optionally TLS on port 3496) and answers the entity's
  alive checks. The Diagnostics and Flashing tabs work as for CAN, with the ECU's logical target address.
  **Status** shows the entity status and diagnostic power mode. A simulated entity (a gateway with a simulated
  ECU) works without a vehicle. Test scripts use `can_explorer.core.doip` (`discover`, `DoipClient` with a UDS
  transport per target address, `DoipEntitySimulator`).
- **UDS discovery** page of a connection: an interactive UDS scanner for reverse engineering, finding the
  services, data identifiers (ReadDataByIdentifier), subfunctions and routines an ECU exposes, and a byte sweep of
  a request template. It sends a probe for every value in a range over the Diagnostics transport (ISO-TP, LIN or
  DoIP) and lists the ones the ECU answers, classified as supported, locked (security access), wrong state/session
  or rejected; a found request can be opened in the Diagnostics view. Read-only by default; the byte sweep and
  starting routines need an explicit opt-in. Scripts use `can_explorer.core.uds_scan` (`scan_services`,
  `scan_data_identifiers`, `scan_subfunctions`, `scan_routines`, `sweep`).
- **Bus gateway** tool: forwards frames received on one connection onto another, with a filter, optional
  identifier rewriting (a new identifier or a signed offset) and a per-identifier rate limit, with live
  forwarded/dropped counters per rule. Only received frames are forwarded and an echo guard prevents loops between
  mirror rules. Works between any open connections, including the simulated interface. Scripts use
  `can_explorer.core.gateway` (`Gateway`, `GatewayRule`).

### Changed

- The simulated UDS ECU (`UdsServer`) answers truncated or malformed requests with
  *incorrectMessageLengthOrInvalidFormat* instead of stopping, as a real ECU does, so it can be scanned.

### Fixed

- MQTT: the retained `offline` status is now published when the bridge is stopped right after connecting (e.g. the
  connection closed again at once), where a cancellation while still subscribing could skip it.
- `tools/benchmark.py` measures the trace model as it works now (following the trace store) instead of
  failing.

## [1.1.1] – 2026-10-20

### Fixed

- The installers contain the ETAS BOA (`etas`), IXXAT (`ixxat`) and USB2CAN (`usb2can`) interfaces. These
  backends load their vendor driver when imported, so a build machine without the driver left them out, and their
  devices were neither detected nor usable although the interfaces were listed.
- The Physical I/O tool was shown in the middle of the window, header included, until a device was opened.

## [1.1.0] – 2026-10-20

### Added

- **Snap** (`snap/snapcraft.yaml`): the release workflow builds a strictly confined snap from the Linux bundle and
  publishes it to the Snap Store for release tags (stable; candidate for `-rc` tags, beta for other pre-releases).
  SocketCAN and USB adapters work after `snap connect canexplorer:can-bus` and `canexplorer:raw-usb`. The update
  dialog leaves snaps to snapd.
- **MQTT**: publishes the decoded signals of a connection (`PREFIX/CONNECTION/signals/MESSAGE/SIGNAL`, when they
  change, at most once per interval) and optionally its frames to an MQTT broker, with an online/offline status
  and automatic reconnection (**MQTT…** in the connection's toolbar). Optionally sends the frames that other
  programs publish to `…/send`, limited to an allow list. `can-server` publishes its buses the same way
  (`--mqtt`, `--database`, `--mqtt-frames`, `--mqtt-send`). Needs the `mqtt` extra (aiomqtt), which the installers
  contain.
- **EtherCAT (CoE) tool**: an EtherCAT master for configuring slaves through CANopen over EtherCAT. It finds the
  slaves of a network adapter, brings them to Init, Pre-Op or Safe-Op (reporting the AL status of slaves that
  refuse), reads and writes their objects over SDO with the object dictionary read from the slave (SDO
  information) or an EDS file, reads the PDO assignment and mapping with the mapped objects' values, and collects
  emergency messages. Real adapters need the new `ethercat` extra (pysoem); the installers do not include it and
  offer the simulated network, which works without hardware. Cyclic process data (Op) is not exchanged.
- `BIT1` to `BIT8` data types in object dictionaries.
- **Physical I/O tool**: switches the digital outputs of a USB I/O adapter and shows its digital inputs and
  analogue values (in volts with a configurable reference, with a short chart). Lines are made inputs (no pull,
  pull-up, pull-down, active low) or outputs. Supports aio_usb adapters (the I/O interface of STM32 CAN/LIN/I/O
  adapter firmware) through the new `io` extra (pyusb, libusb), and a simulated device that works without hardware.
  Test scripts use the same devices through `can_explorer.core.physical_io`.
- **GVRET interface** (`gvret`, class `GvretBus`) for devices with SavvyCAN's GVRET protocol: ESP32RET boards,
  the Macchina M2 (M2RET) and the CANDue. The channel is the serial port, with `@1` for the device's second bus,
  or `socket://IP:23` for ESP32RET on WiFi; the bitrate (and listen only mode) of the bus is set when it opens,
  leaving the other bus as it is. Classic CAN only.
- **J2534 interface** (`j2534`, class `J2534Bus`) for vehicle interfaces with an SAE J2534 04.04 driver, such as
  the Drew Technologies Mongoose and CarDAQ and the Tactrix OpenPort 2.0 (Windows). The channel is the registered
  device name or the driver's path; detection lists the registered drivers with CAN. 32-bit drivers, which the
  64-bit application cannot load, run in the J2534 bridge, a 32-bit helper program in the Windows installer (or a
  32-bit Python when run from source, or the program set in `CANEXPLORER_J2534_BRIDGE`).
- **ELM327 interface** (`elm327`, class `Elm327Bus`) for OBD adapters with the ELM327 command set, such as the
  OBDLink MX+, LX, SX and EX, over USB, Bluetooth (the paired adapter's serial port) or WiFi (`socket://IP:35000`).
  Made for diagnostics: it monitors the bus and sends frames between monitoring, at 500 kbit/s divided by a whole
  number, and loses frames on busy buses. Detection lists the USB and Bluetooth serial ports.

### Fixed

- Loading a signal database while a simulation runs restarts the simulation. The nodes look up their messages when
  they start, so a simulation started before the database stayed broken (*Load a signal database to send message
  'VehicleSpeed' by name*) until it was restarted by hand. `can-test` warns when `--simulation` is used without
  `--database`.
- The installers contain the `canexplorer_remote` interface (remote buses of `can-server`) and pyserial's URL
  handlers such as `socket://`; python-can imports them by name, so the bundles left them out.
- Channel detection probes each interface on its own: an error in one driver (python-can lets it end the whole
  detection) no longer hides the channels of all others, and slow drivers such as ETAS BOA get 15 seconds instead
  of 5, so ETAS devices are listed on Windows.

### Changed

- Builds of `master` published to GitHub Packages are packaged with PyInstaller again instead of Nuitka;
  Nuitka stays selectable when starting the `release` workflow manually.

## [1.0.0] – 2026-10-18

The first stable release. Highlights since 0.0.1:

- A dashboard-style interface (QML) on an asyncio core, with six themes, a command palette (Ctrl+K) and German
  texts, handling hundreds of thousands of frames per second and recordings of millions of frames.
- Recording in every replayable format (ASC, BLF, CSV, LOG, TRC, SQLite, MF4, gzip), triggered and split
  recordings, replays with seeking and replay onto a bus, and comparing two recordings.
- Signal databases (DBC, DBF, KCD, SYM, ARXML, LDF) with an editor for CAN and LIN databases, signal plots with
  cursors, and the multi-bus trace for several buses on one timeline.
- Diagnostics and protocol tools: UDS over ISO-TP and LIN, ECU flashing, J1939 and CANopen tools, bus health.
- Simulation and test automation: Python nodes, gateways and QML panels; test scripts with requirements, run in
  the application or headless in CI with `can-test`.
- Remote buses through `can-server`, installers for Windows, Linux and macOS, and a documentation site.

From 1.0.0 on, CANExplorer follows semantic versioning; the public interfaces are listed under
[Stability and versioning](https://akj7.github.io/CANExplorer/reference/#stability-and-versioning).

### Added

#### Connections and buses

- **All CAN interfaces of python-can 4.6** (SocketCAN, PEAK, Vector, Kvaser, IXXAT, ETAS, neoVI, NI-XNET, NI-CAN,
  SYS TEC, USB2CAN, isCAN, Neousys, CANalyst-II, CANtact, gs_usb, slcan, serial, Robotell, Seeed Studio,
  socketcand, UDP multicast, virtual) and the new remote bus, listed with their hardware, platforms, drivers and
  CAN FD support in the *Supported interfaces* reference and the README. The `drivers` extra
  (`uv sync --extra drivers`) installs the Python packages the neoVI, NI-XNET, CANalyst-II, CANtact, gs_usb and
  UDP multicast interfaces need.
- Serial CAN adapters (slcan, python-can's serial protocol) work out of the box: `python-can[serial]` installs
  pyserial.
- CAN FD data bitrate setting.
- Channel detection for all python-can interfaces, run in the background.
- **Remote buses**: `can-server` shares the CAN interfaces of a computer (e.g. a Raspberry Pi in a vehicle) over
  WebSocket, with an optional token; CANExplorer connects with the new *RemoteBus* interface (python-can interface
  `canexplorer_remote`, also usable by `can-test` and scripts).
- **Several buses in one view**: the *Multi-bus trace* tool merges the traces of chosen connections on one
  timeline with a Bus column, each frame decoded with its own connection's protocol and database. Simulation nodes
  receive and send on other connections (`bus='Body'` in `@on_message`, `@on_signal`, `send` and `message`), and
  the new `Gateway` node forwards frames between two buses, optionally changed by `transform`, without loops.
- Hardware timed periodic transmission (python-can cyclic tasks) as an alternative to the application timer.

#### Monitoring and analysis

- **Dashboard user interface** modelled on the learnqt.guide dashboards: a sidebar with the connections and the
  views of the selected connection, a top bar with breadcrumb, filter field, connect button, notifications and
  the application menu, rounded cards throughout and a welcome page with quick actions and recent connections.
  The new **Dashboard** view shows Rx/Tx frame rates, bus load and error frames as cards with one-minute
  sparklines and trends, a bus activity area chart, a traffic breakdown donut chart and the busiest identifiers.
  Six colour themes (Dark, Purple, Nord, Ocean, Forest, Light) are selectable in the preferences. The menu bar
  and the connection tabs are replaced by the sidebar and the **⋮** menu, and errors appear as notifications
  instead of modal dialogs. The window is frameless: the top bar moves it and holds the minimize, maximize and
  close buttons, and the edges resize it.
  A custom Qt Quick Controls style (`CanExplorerStyle`) draws all buttons, text fields, combo boxes, spin boxes,
  check boxes, switches, dialogs, menus, tooltips, tabs, scroll bars and sliders in the theme colours.
- **Bus health** view: error state (active, warning, passive, bus off) with a timeline, error frames, TEC/REC
  error counters and bus load over the last minute, ten minutes or hour, and the decoded error events. Error frames
  (SocketCAN layout) are decoded in the trace as well.
- **Bus statistics**: Rx/Tx frame rates, estimated bus load, error frames and bus state in the status bar.
- **Trace search** (Ctrl+F, F3/Shift+F3) for text, identifiers, data patterns (`data=01 ?? FF`) and signal
  conditions (`EngineData.EngineSpeed > 3500`), in the background through millions of frames; **bookmarks**
  (Ctrl+B) with notes, a bookmark list and markers; a context menu on trace rows.
- **Overview sorting**: clicking a column header sorts the overview by that column (ascending, descending, back to
  identifier order); the order is kept while frames arrive, and selections follow their rows. Bytes that differ
  from the previous frame of an identifier are highlighted in colour as well as bold.
- **Long recordings**: the trace keeps the most recent frames in memory and pages older ones to temporary files
  on disk (5 million frames by default, in the user's cache directory), so millions of frames can be scrolled,
  filtered, searched and exported; preferences for both limits and the directory.
- Live **signal plots** of database signals over a selectable time window.
- **Signal plot analysis**: zoom (mouse wheel) and pan (drag) over the whole signal buffer (200,000 points per
  signal), fixed live windows of 5 s to 5 min and *Fit all*; two **cursors** (click, Shift+click) with the values
  at both cursors, the difference and the minimum, maximum and mean between them; several signals per chart,
  each with its own scale or a shared one; **CSV export** of all or the visible range.

#### Recording and replay

- **More log formats** for recording, export, replay and replay on the bus: ASAM **MF4** (with the optional `mf4`
  extra, which installs asammdf) and gzip compressed text logs (`.asc.gz`, `.csv.gz`, `.log.gz`, `.trc.gz`). One
  list of formats (`core/log_formats.py`) serves all of them, so every recordable format can be replayed; the file
  dialogs name each format and add the extension of the chosen one.
- **Recording options**: splitting into numbered files by size and/or time, and **triggered recording** with start
  and optional stop conditions, time kept before and recorded after the trigger, and re-arming.
- **Replay controls**: replays of log files can be paused and resumed and their speed changed while they run
  (0.25× to 10×, or as fast as possible), with a playback bar showing position, length and frames. The replay
  dialog asks for the speed instead of "reproduce original timing".
- **Jumping within replays**: the playback bar has a position slider and 10 s back/forward buttons; "Replay from
  here" in the context menu of a trace row continues a replay at that frame. Forward jumps pass the frames in
  between at once (statistics stay complete), backward jumps read the recording again; replays on the bus skip
  the frames jumped over.
- **Replay on the bus**: "Replay to bus…" transmits the frames of a log file on a connected bus with their recorded
  timing, at 0.25× to 10× or as fast as possible, optionally filtered and repeated, with a playback bar (progress,
  pause, speed, stop). Errors stop it with a notification.
- **Comparing recordings** (*Tools* → *Compare recordings*): two log files side by side — identifiers only in
  one of them, changed cycle times, lengths and payloads, paired by start time, by the first frame of an
  identifier or by frame sequence; with a signal database, value ranges, means and differences of every signal;
  CSV export.

#### Signal databases

- **Database editor** (Tools → Database editor, ⋮ → Edit Signal Database…, or "Edit database" on a connection):
  create and edit DBC, DBF, KCD and SYM databases, and ARXML databases saved as one of them. Messages
  (name, identifier, length, CAN FD, cycle time, senders, comment), signals (position, byte order, sign, factor,
  offset, range, unit, initial value, receivers, value descriptions, comment) and nodes; a colour coded bit layout;
  checks for overlapping or out-of-frame signals and duplicate identifiers; notes on what the target format cannot
  store; confirmation before discarding unsaved changes. Connections decoding with a saved file reload it.
- **LIN database editing**: LDF files open in the database editor with tabs for frames and signals, nodes with
  their node attributes, encoding types and schedule tables. A new LDF writer saves all sections of LIN 2.x files
  (diagnostic frames, sporadic and event triggered frames, signal groups, all schedule commands); saved files are
  checked by reading them back. **New ▾** creates CAN or LIN databases.
- **BUSMASTER DBF** signal database reader and writer, and conversion between DBC, DBF, KCD, SYM and ARXML
  (input only), in the application (File → Convert Signal Database) and as the `can-db-convert` command.
  DBF and LDF files can be loaded as signal databases.
- **ARXML** container frames (container I-PDUs) are decoded into their contained PDUs, whose signals can be plotted.
- Databases describing several buses (ARXML clusters, KCD buses): the bus of a connection is selected from its
  bitrate, or chosen in the toolbar, so identifiers reused on other buses are decoded correctly.
- `can-db-convert --bus` converts one bus of a multi-bus database.

#### Diagnostics and protocols

- **UDS diagnostics** (ISO 14229) over ISO-TP on CAN and over the LIN transport layer: diagnostic session
  control, ECU reset, tester present keep-alive, read/write data identifiers with names and readable
  values for the standard identifiers, security access with a user supplied seed/key algorithm (a Python file
  defining `compute_key(level, seed)`), reading DTCs with status flags and clearing them, routine control,
  raw requests and a request history. Handles "response pending" and suppressed positive responses.
- **CDD** (Vector CANdelaStudio) diagnostic descriptions, loaded in the Diagnostics tab or with "Load database…": the
  ECU's data identifiers are listed by name and ReadDataByIdentifier responses are decoded into fields (scaling,
  units, text tables, text and float encodings, structures and unions) in the Diagnostics tab and in the UDS and LIN
  trace decoders. Uses an own reader, as cantools' CDD support is deprecated.
- Passive "UDS" protocol decoder describing the services carried in ISO-TP messages.
- Active ISO-TP client (ISO 15765-2) with flow control, block size, separation time and CAN FD.
- **ECU flashing**: the **Flashing** view programs ECUs over UDS from Intel HEX, S-record or binary files:
  programming session, security access, erasing, RequestDownload/TransferData/RequestTransferExit per segment
  with the ECU's block size, a check routine and an ECU reset, with progress, transfer rate and cancelling. The
  simulated ECU accepts downloads; `examples/flashing` has an example firmware and the simulator's key file.
- **J1939 tools**: the **J1939** view of J1939 connections lists the nodes with their address claims (NAME
  fields), lamps and trouble codes; shows the DM1 and DM2 codes of a node with SPN names and FMI texts; requests
  address claims and DM2, clears codes with DM11/DM3; and sends any parameter group by name, number or database
  message from hex data or signal values, with BAM or RTS/CTS for more than 8 bytes. The trace decodes address
  claims and DM1/DM2, also after transport protocol reassembly.
- **CANopen master tools**: the **CANopen** view of CANopen connections lists the nodes with their NMT state and
  emergency messages; sends NMT commands and SYNC; loads object dictionaries from EDS and DCF files; reads and
  writes objects over SDO (expedited and segmented, with abort codes explained) in their data types; and shows the
  PDO mappings, from the dictionary or read from the node, with the received values. The trace names SDO objects
  and decodes PDO values with the dictionary. `CanOpenNodeSimulator` simulates a node from an EDS file, and
  `examples/canopen/io_module.eds` describes an example I/O module.
- **LIN** support through SocketCAN LIN drivers following the sllin convention (header = RTR frame,
  response = data frame), selectable as bus type in the connection dialog:
  - Passive LIN decoder: headers and responses with protected identifiers, diagnostic transport frames
    (0x3C/0x3D) with reassembly, LIN node configuration services and UDS.
  - LDF (LIN description file) signal decoding and signal plots, via `ldfparser`.
  - LIN master: runs LDF schedule tables, with editable values of the signals the master publishes.
  - Header-only transmission, LIN bus load estimation, protected identifier and checksum helpers.

#### Simulation and test automation

- **Node simulation and scripting** (Simulation tab): Python scripts define simulated nodes (`Node` subclasses)
  with handlers for start/stop, received messages (by identifier or database name), signal changes and shared
  variables, timers, sending raw frames or database messages by signal name, and a console. Script errors are
  contained and reported. QML panels get a `panel` object with live signal and variable values, sending and
  logging, and drive the nodes through shared variables. Documentation in `docs/scripting.md`, example in
  `examples/simulation`.
- Simulated UDS ECU and LIN slaves (`can_explorer.core.simulation`) to try the application without hardware.
- **Test automation**: test scripts (`TestModule` classes with `@testcase` methods) check ECUs on the bus with a
  test context offering checks (soft and required), sending by database name or identifier, waiting for frames and
  signal values, cycle time measurement, UDS requests and the simulation's variables. Verdicts passed, failed,
  error (exceptions, timeouts, failing setup) and skipped. They run in the new **Tests** view of a connection (select,
  run, stop, live results and logs) or headless with `can-test` (exit code for CI, `--simulation` to test against
  simulated nodes), with HTML and JUnit XML reports. Example in `examples/testing`.
- **Test automation for CI**: parametrised test cases (`@testcase(params=…, ids=…)`), requirement identifiers per
  test case or module with a requirements table in the HTML report, JUnit properties and `can-test -r REQ`,
  Markdown summaries (`--markdown`, e.g. `$GITHUB_STEP_SUMMARY`), and the example test suite runs against the
  example simulation in the build workflow.

#### User interface

- **Command palette** (Ctrl+K, Ctrl+Shift+P): every command, view, tool, connection, theme and language by typing
  a few letters.
- **Translations**: the interface can be switched to German in the preferences or the command palette, at once
  without restarting; translation files are Qt Linguist `.ts` files kept up to date by
  `tools/update_translations.py`.
- **Workspaces** (`.cxw`): connections with their bus settings or replayed files, databases, filters, plotted
  signals, simulation scripts and panels and test scripts, saved in one file with relative paths and reopened
  with one click (Ctrl+S, Ctrl+Shift+O, recent workspaces on the welcome page).
- **Glass dialogs**: dialogs, menus, popups, combo box lists and the notifications are frosted glass — the window
  content behind them blurred under a translucent theme tint, with a light edge, a sheen and a soft shadow. With
  Qt's software renderer they fall back to opaque panels. The documentation screenshots are rendered with OpenGL.
- **Movable dialogs**: dragging the title of a dialog moves it, within the window; it opens centred again.
- **Resizable sidebar**: drag its right edge (180–420 pixels, at most half the window), double-click to restore the
  default; the width is remembered. The top bar's breadcrumb and filter field shrink in narrow windows instead of
  pushing the window buttons out.
- **Logo** with a transparent background: shown in the sidebar, on the welcome page and in the About dialog, as
  window icon, as icon of the PyInstaller and Nuitka bundles (`.ico`, `.icns`, `.png`) and as logo and favicon of
  the documentation, all generated from the artwork `docs/assets/logo.png` by `tools/make_icons.py`.
- **Desktop icon on Linux**: `--install-desktop-entry` adds CANExplorer with its icon to the desktop's application
  menu (`--uninstall-desktop-entry` removes it), and the window is matched to that entry.
- Recent connections and log files, a preferences dialog (trace buffer size, view refresh interval) and
  remembered window geometry.
- Row selection in the tables, Edit → Copy (tab separated) and Select All, and export of the selected or
  displayed trace rows to a log file.

#### Packaging, documentation and development

- **Updates**: CANExplorer looks for new GitHub releases at start (at most daily, can be turned off, optionally
  including pre-releases) or on request (⋮ → Check for Updates…), shows the release notes, and downloads,
  verifies (`SHA256SUMS.txt`) and installs the update for the way it was installed: Windows setup, AppImage,
  Debian package or macOS app; it then restarts. Versions can be skipped.
- **Installers**: `packaging/installer.py` creates a Windows setup (Inno Setup), a Linux AppImage and `.deb`
  package and a macOS disk image from the bundle. The new `release` workflow (replacing `installer`) builds them
  on the three platforms, installs each to check it starts, stores them as artifacts and publishes them with
  checksums as a GitHub release for `v*` tags, after checking the tag matches the project version.
  Builds of `master` are compiled with Nuitka and published to GitHub Packages
  (`ghcr.io/akj7/canexplorer-installers`, via ORAS).
- Nuitka as an alternative packaging backend (`packaging/build.py --backend nuitka [--onefile]`), selectable
  in the installer workflow.
- **Documentation site** built with MkDocs and Material for MkDocs (`docs/`, `mkdocs.yml`): installation, quick start,
  a user guide for every part of the application, reference pages (scripting API generated from the docstrings, QML
  panel API, command line, file formats, keyboard shortcuts) and development guides, with screenshots generated
  from the running application (`tools/docs_screenshots.py`). The `docs` workflow builds it strictly and publishes
  it to GitHub Pages.
- Documentation in the **Dracula** colour scheme (`docs/stylesheets/dracula.css`, on top of Material for MkDocs).
- Throughput benchmark of the receive pipeline and the table models (`tools/benchmark.py`).
- Test fixtures and tests for ARXML, KCD, SYM and CDD files.

### Changed
- **The application command is now `canexplorer`** (was `main`, which clashed with other tools on the PATH):
  `uv run canexplorer`, `canexplorer --install-desktop-entry`.
- The default log level is `INFO` (was `DEBUG`); the unused `debug_mode` setting was removed.
- `can-server` and `RemoteBus` check that they speak the same protocol version and say which side to update.
- The Docker setup (`Dockerfile`, `docker-compose.yml`) provides a development shell, the test suite and a
  `can-server` service sharing the host's CAN interfaces.
- Project metadata: description, classifiers and project URLs; `pydantic` is no longer a direct dependency (it
  comes with `pydantic-settings`).
- Receive pipeline about 4–7× faster (70 000 → 300 000 frames/s raw, 46 000 → 280 000 frames/s with J1939
  and DBC decoding): frames are handed from the receive thread to the event loop in batches, log replay is
  read in chunks, signal databases are only decoded for displayed rows and the result is cached, and the
  changed-byte highlighting is computed on demand.
- The overview only refreshes the rows that changed; rebuilding an unfiltered trace is about 20× faster.
- Diagnostics moved into a dedicated controller with a reworked Diagnostics tab.
- `MainApplication` simplified; `canutils` reduced to the helpers in use.
- Runtime dependencies trimmed to what is imported: removed `matplotlib`, `numpy`, `qtrio`, `schema` and
  `pytest-schema`; `pyinstaller` and `nuitka` moved to a `packaging` dependency group and `rust-just` to the
  `dev` group. Added `ldfparser` (pinned) for LIN.
- Builds without package metadata (Nuitka) carry the application version in a generated module.

### Removed
- The unused `can_server` heartbeat module, which needed the undeclared `aiofile` package.
- The AsciiDoc documentation template (`docs/full_doc.adoc`) and the outdated `docs/todo.md`; the references moved
  to the documentation site.
- The unused `can_explorer/transport` package (IsoCAN, ISO-TP, J1939 and CANopen transports), superseded by
  the decoders and clients in `can_explorer/core`, together with its helpers (`util/worker.py`,
  `util/validator.py`) and tests.
- Obsolete version helpers (`util/version.py`, `sys/version.py`), the empty `database` package, the crash
  handler stub, the placeholder `CustomComponent.qml` and the trivial demo test.
- Empty `publish`, `release` and `security` workflows.

### Fixed
- ISO-TP transfers with separation times below the system timer resolution (e.g. 100 µs) took about 15 ms per
  frame on Windows; frames are now paced with a precise clock.
- Table columns (trace, overview, multi-bus trace) can be resized by dragging the header edges.
- Drop-down lists of combo boxes with named entries (e.g. the language) showed empty rows.
- The About dialog named the license as GPL3; CANExplorer is MIT licensed.
- Connecting to an unreachable `can-server` reported different messages on Windows and Linux.
- A binding loop in the dialog style (an elided dialog title).
- Converting a multi-bus database or one with container frames lost information silently; it is now reported,
  and the signal count includes the signals of contained PDUs.
- The QML window test fixture could crash the test process at exit.
- The GUI could be starved under sustained traffic because the session never yielded to the event loop.
- Default bitrate and data bitrate in the connection dialog were not preselected.
- Channel detection printed a warning for every missing driver and was left pending when quitting.
- `.vscode/extensions,json` renamed to `extensions.json`.

## 2026-10-18 – Asyncio core, QML user interface and tooling (`5bc1953`)

### Added
- Qt-independent asyncio core (`can_explorer/core`): message sources for live buses (`LiveSource`) and log
  file replay (`FileSource`), `BusSession` with a bounded trace buffer, per-identifier statistics, message
  filters, recording and decoding.
- Qt and asyncio run on one event loop (qasync); views are refreshed in batches about 30 times per second.
- Trace and per-identifier overview views (count, cycle time, changed bytes highlighted).
- Display filters: hex identifiers and ranges, exclusions and `std`/`ext`/`rx`/`tx`/`fd` keywords.
- Recording to `.asc`, `.blf`, `.csv`, `.log`, `.trc` and `.db`, and replay with or without the original timing.
- DBC/KCD/SYM/ARXML signal decoding with cantools.
- Transmission of single and periodic frames.
- Passive protocol decoders: ISO-TP (with reassembly), J1939 (PGNs, requests, BAM and RTS/CTS transport)
  and CANopen (NMT, SYNC, EMCY, PDO, SDO, heartbeat).
- Basic UDS requests over ISO-TP in a Diagnostics tab.
- PyInstaller packaging (`packaging/build.py`) and an installer workflow building Linux, Windows and macOS
  bundles and attaching them to GitHub releases for `v*` tags.
- CI workflow running ruff, black and pytest on Linux, Windows and macOS with Python 3.10 and 3.14.
- Test suite for the core, the decoders, the Qt models and the QML window, using python-can's virtual bus.

### Changed
- Package management moved from Poetry to uv (PEP 621 metadata, hatchling build backend, `uv.lock`).
- User interface fully moved from Qt Widgets (`.ui` files) to QML: main window, new connection dialog,
  about dialog with copy to clipboard, trace table.
- Settings read `CANEXPLORER_*` environment variables and parse command line arguments only when the
  application starts.
- The application version is read from the package metadata instead of `git describe`.
- Code formatted with black, linted with ruff.

### Removed
- Qt Widgets user interface (`.ui` files, widget classes, stylesheet) and the thread-pool workers.

### Fixed
- Resource paths depended on the working directory.
- The application aborted when started from the VS Code snap terminal (GSettings schema error caused by
  the snap's GTK environment).
- `Settings` parsed foreign command line arguments when imported (e.g. by pytest).
- Recording to BLF could fail for channel names ending in digits and stopped the whole session; a failing
  recording now only stops the recording.
- Closing a bus blocked the GUI for up to one second.
- Import-time asyncio usage that fails on Python 3.14.
- `CanMessage.from_can` copied `is_fd` into `is_extended_id`.

## 2025-11-30 – QML support (`c6342fc`)

### Added
- QML main window (`ApplicationWindow`) with File, Edit and Help menus, shared actions and an about dialog.
- `MainApplication` hosting the QML engine; QML warnings and Qt messages routed to the Python logger.
- Application metadata (name, version, build information) exposed to QML.
- Configuration with pydantic-settings and coloured console logging.

## 2025-10-13 – Resume project (`392563d`)

### Added
- J1939 addressing and controller applications in the J1939 transport.
- Asyncio based worker base class.
- IsoCAN transport tests on python-can's virtual bus.

### Changed
- IsoCAN transport reworked on top of asyncio.

## 2025-02-10 – J1939 transport (`f0e30fb`)

### Added
- J1939 PDU parsing and frame information (identifier length, arbitration type).

## 2025-02-08 – Refactoring (`8ee4442`)

### Added
- First QML main window.
- J1939 and CANopen transport skeletons and a J1939 test.
- `can_server` package (aiohttp based server).

### Changed
- Transports reorganised into packages (`isocan`, `isotp`, `j1939`, `canopen`).

## 2025-02-08 – Poetry (`589ec5f`)

### Changed
- Package management moved from Pipenv to Poetry.
- Docker based development container and docker-compose setup; justfile for development tasks.

## 2024-11-10 – ISO-TP (`16b66cb`)

### Added
- ISO-TP processing according to ISO 15765-2 (work in progress): addressing modes, PDU parsing, error
  codes and the transport state machine.
- `CanMessage` model.

## [0.0.1] – 2024-10-21

Called "Milestone 0.1.0" in the commit message and the README roadmap.

### Added
- IsoCAN frames are received through python-can and displayed in the GUI.
- Environment based configuration (`.env`).

## 2024-10-16 – Initial commit (`a8d624b`)

### Added
- Qt Widgets user interface: main window, new connection dialog, about dialog and a raw CAN frame table.
- Pipenv based project setup, VS Code development container, Dockerfile and a CI workflow skeleton.

[Unreleased]: https://github.com/AKJ7/CANExplorer/compare/v1.8.0...HEAD
[1.8.0]: https://github.com/AKJ7/CANExplorer/compare/v1.7.1...v1.8.0
[1.7.1]: https://github.com/AKJ7/CANExplorer/compare/v1.7.0...v1.7.1
[1.7.0]: https://github.com/AKJ7/CANExplorer/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/AKJ7/CANExplorer/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/AKJ7/CANExplorer/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/AKJ7/CANExplorer/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/AKJ7/CANExplorer/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/AKJ7/CANExplorer/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/AKJ7/CANExplorer/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/AKJ7/CANExplorer/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/AKJ7/CANExplorer/releases/tag/v1.0.0
[0.0.1]: https://github.com/AKJ7/CANExplorer/releases/tag/0.0.1
