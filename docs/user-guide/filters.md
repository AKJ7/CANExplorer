# Filters

The filter field in the top bar limits the frames shown in the trace and the overview. Recording, statistics,
plots, diagnostics and simulations always see all frames. An invalid filter is shown in red and has no effect;
hover it to see the error.

## Syntax

Terms are separated by spaces or commas. An empty filter shows everything.

| Term | Meaning |
|---|---|
| `100` or `0x100` | Identifier 0x100 (identifiers are hexadecimal) |
| `100-1FF` | Identifiers 0x100 to 0x1FF |
| `!123`, `!700-7FF` | Exclude identifiers or ranges |
| `std`, `ext` | Only standard or only extended identifiers |
| `rx`, `tx` | Only received or only transmitted frames |
| `fd` | Only CAN FD frames |

Several identifiers and ranges are combined with *or*; keywords and exclusions further restrict the result.

## Conditions

Searching the trace and triggered recordings use *conditions*, which extend the filter syntax:

| Condition | Matches |
|---|---|
| `7E8`, `100-1FF !123 ext rx` | Frames matching these filter terms |
| `data=01 ?? FF` | Frames whose data starts with 01, any byte, FF |
| `7E8 data=03 7F` | Filter terms and a data pattern together |
| `EngineData.EngineSpeed > 6000` | Frames of a database message whose signal compares as given: `==`, `!=`, `<`, `<=`, `>`, `>=` with physical values |
| `EngineData.GearPosition == Second` | Comparing the value table text of a signal (`==` and `!=` only) |

The message of a signal comparison can also be a contained PDU of an AUTOSAR container frame.

## Examples

| Filter | Shows |
|---|---|
| `7E0-7EF` | OBD/UDS diagnostic traffic |
| `ext rx` | Received frames with extended identifiers |
| `100-1FF !123` | 0x100–0x1FF except 0x123 |
| `tx fd` | CAN FD frames sent by CANExplorer |
| `18FEF100-18FEF1FF` | J1939 PGN 0xFEF1 from any source address |
