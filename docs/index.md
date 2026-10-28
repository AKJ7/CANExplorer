<p align="center" markdown>
  ![CANExplorer logo](assets/logo-banner.png){ width="420" }
</p>

# CANExplorer

CANExplorer is a cross-platform tool to monitor, analyse, diagnose and simulate **CAN**, **CAN FD** and **LIN**
buses. It runs on Linux, Windows and macOS with any interface supported by
[python-can](https://python-can.readthedocs.io/), and without hardware on a virtual bus.

![CANExplorer showing a trace with decoded signals](assets/screenshots/trace.png)

## Features

<div class="grid cards" markdown>

-   :material-monitor-dashboard: **Monitoring**

    ---

    Trace and per-identifier overview, display filters, bus load and error statistics, pause, copy and export.

    [:octicons-arrow-right-24: Trace and overview](user-guide/trace-and-overview.md)

-   :material-database: **Signal databases**

    ---

    Decode signals with DBC, BUSMASTER DBF, AUTOSAR ARXML, KCD, SYM and LIN LDF files, plot them live and
    convert between the formats.

    [:octicons-arrow-right-24: Signal databases](user-guide/signal-databases.md)

-   :material-record-rec: **Recording and replay**

    ---

    Record to ASC, BLF, CSV, LOG, TRC, SQLite and MF4 files, replay them with their original timing and compare
    two recordings.

    [:octicons-arrow-right-24: Recording and replay](user-guide/recording-and-replay.md)

-   :material-stethoscope: **Diagnostics**

    ---

    UDS over ISO-TP and the LIN transport layer, with DTCs, security access, CANdelaStudio CDD and ASAM ODX/PDX descriptions,
    ECU flashing, and GMLAN (GMW3110) diagnostics for GM/Opel vehicles.

    [:octicons-arrow-right-24: Diagnostics](user-guide/diagnostics.md)

-   :material-transit-connection-variant: **Protocols**

    ---

    Decoding of ISO-TP, UDS, J1939, CANopen and LIN; J1939 network and trouble code tools, a CANopen master and
    LIN master schedule tables.

    [:octicons-arrow-right-24: Protocol decoding](user-guide/protocols.md)

-   :material-script-text: **Simulation and scripting**

    ---

    Simulated bus nodes in Python and control panels in QML, like CAPL nodes and panels.

    [:octicons-arrow-right-24: Simulation and scripting](user-guide/simulation.md)

</div>

## Where to start

- New to CANExplorer? Follow the [quick start](getting-started/quick-start.md): it needs no hardware.
- Looking for a specific feature? The [user guide](user-guide/index.md) describes every part of the application.
- Writing simulation scripts or panels? See the [scripting API](reference/scripting-api.md) and the
  [QML panel API](reference/panel-api.md).
- Working on CANExplorer itself? Start with the [architecture](development/architecture.md).

CANExplorer is open source, developed on [GitHub](https://github.com/AKJ7/CANExplorer).
