"""Command line converter between signal database formats.

Usage: can-db-convert INPUT OUTPUT [--protocol CAN|J1939] [--bus NAME]

The formats follow the file extensions: .dbc, .dbf (BUSMASTER), .kcd, .sym and, as input only, .arxml.
"""

import argparse
import sys

from can_explorer.core.database_io import READ_SUFFIXES, WRITE_SUFFIXES, DatabaseFormatError, convert_database
from can_explorer.core.dbf import DbfError


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog='can-db-convert',
        description='Convert CAN signal databases, e.g. between DBC and BUSMASTER DBF.',
        epilog=f'Input formats: {", ".join(READ_SUFFIXES)}. Output formats: {", ".join(WRITE_SUFFIXES)}.',
    )
    parser.add_argument('input', help='database to read')
    parser.add_argument('output', help='database to write (format from the extension)')
    parser.add_argument(
        '--protocol',
        choices=['CAN', 'J1939'],
        help='protocol of a DBF output (default: J1939 if the DBC declares ProtocolType "J1939", else CAN)',
    )
    parser.add_argument('--bus', help='convert only this bus of a database describing several (ARXML cluster, KCD bus)')
    args = parser.parse_args(argv)
    try:
        report = convert_database(args.input, args.output, args.protocol, args.bus)
    except (OSError, DatabaseFormatError, DbfError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    except Exception as e:  # cantools raises various parser errors
        print(f'error: could not convert {args.input}: {e}', file=sys.stderr)
        return 1
    print(report.summary())
    return 0


if __name__ == '__main__':
    sys.exit(main())
