"""Label files: what a manufacturer's diagnostic data means, supplied by you — module names, measuring-block
formulas and value names, and coding records with their fields. CANExplorer ships none of this: the meanings are
the manufacturer's own and undocumented, and community tables disagree, so a label file is your transcription of
the source you trust. (Ross-Tech's label files are not read.)

A label file is JSON:

    {
      "formulas": {"0x01": {"expression": "a * b / 5", "unit": "/min"}},
      "modules": {
        "01": {
          "name": "Engine",
          "formulas": {"0x10": "a + b"},
          "groups": {"1": ["Engine speed", "Coolant temperature"]},
          "coding": {
            "Coding": {
              "protocol": "kwp", "identifier": "0x9A", "length": 3,
              "fields": [
                {"name": "Option A", "byte": 0, "bit": 0, "values": {"0": "off", "1": "on"}},
                {"name": "Variant", "byte": 1, "bit": 4, "width": 4},
                {"name": "Region", "byte": 2}
              ]
            }
          }
        }
      }
    }

The values are illustrative only. Module keys are logical addresses in hex (`"01"`, `"0x17"`); the top-level
`formulas` (and `groups`) apply to every module, a module's own override them. A file with only `formulas` and
`groups` — a measuring-block formula table (`core/measuring_blocks.py`) — is a label file too. Coding records:
see `core/coding.py` (`protocol` "kwp" for a KWP2000 local identifier, "uds" for a UDS data identifier; a field is
a whole `byte`, or `width` bits — default 1 — from `bit`).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from can_explorer.core.coding import CodingError, CodingRecord, parse_record
from can_explorer.core.measuring_blocks import FormulaTable, MeasuringBlockError


class LabelError(ValueError):
    pass


@dataclass
class ModuleLabels:
    address: int
    name: str = ''
    table: FormulaTable = field(default_factory=FormulaTable)
    coding: Dict[str, CodingRecord] = field(default_factory=dict)


def _address(key: Any) -> int:
    text = str(key).strip()
    try:
        value = int(text, 16)
    except ValueError:
        raise LabelError(f'Invalid module address {key!r}; use hex like "01"') from None
    if not 0 <= value <= 0xFF:
        raise LabelError(f'Module address {key!r} is out of range 00-FF')
    return value


@dataclass
class LabelFile:
    table: FormulaTable = field(default_factory=FormulaTable)  # Shared by all modules
    modules: Dict[int, ModuleLabels] = field(default_factory=dict)
    path: Optional[Path] = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> 'LabelFile':
        if not isinstance(data, Mapping):
            raise LabelError('A label file is a JSON object')
        try:
            table = FormulaTable.from_dict({'formulas': data.get('formulas'), 'groups': data.get('groups')})
            modules: Dict[int, ModuleLabels] = {}
            for key, entry in dict(data.get('modules') or {}).items():
                if not isinstance(entry, Mapping):
                    raise LabelError(f'Module {key!r} must be an object')
                address = _address(key)
                coding = entry.get('coding')
                coding = {} if coding is None else coding
                if not isinstance(coding, Mapping):
                    raise LabelError(f'Module {key!r}: "coding" must be an object of named records')
                modules[address] = ModuleLabels(
                    address,
                    str(entry.get('name', '')),
                    FormulaTable.from_dict({'formulas': entry.get('formulas'), 'groups': entry.get('groups')}),
                    {str(name): parse_record(str(name), record) for name, record in coding.items()},
                )
        except (MeasuringBlockError, CodingError) as e:
            raise LabelError(str(e)) from None
        return cls(table, modules)

    @classmethod
    def load(cls, path: str | Path) -> 'LabelFile':
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as e:
            raise LabelError(f'Cannot read the label file {path.name}: {e}') from None
        labels = cls.from_dict(data)
        labels.path = path
        return labels

    def module(self, address: int) -> Optional[ModuleLabels]:
        return self.modules.get(address)

    def module_name(self, address: int) -> str:
        module = self.modules.get(address)
        return module.name if module else ''

    def formula_table(self, address: Optional[int] = None) -> FormulaTable:
        """The measuring-block scaling of a module: the shared formulas and groups, overridden by its own."""
        module = self.modules.get(address) if address is not None else None
        if module is None:
            return FormulaTable(dict(self.table.formulas), dict(self.table.groups))
        return FormulaTable(
            {**self.table.formulas, **module.table.formulas}, {**self.table.groups, **module.table.groups}
        )

    def coding_records(self, address: int) -> List[CodingRecord]:
        module = self.modules.get(address)
        return list(module.coding.values()) if module else []

    def coding_record(self, address: int, name: str = '') -> CodingRecord:
        """A module's coding record by name (or its only one when `name` is empty)."""
        records = self.coding_records(address)
        if not records:
            raise LabelError(f'The label file has no coding record for module 0x{address:02X}')
        if not name:
            if len(records) > 1:
                raise LabelError(
                    f'Module 0x{address:02X} has several coding records; name one: '
                    + ', '.join(r.name for r in records)
                )
            return records[0]
        for record in records:
            if record.name == name:
                return record
        raise LabelError(f'Module 0x{address:02X} has no coding record {name!r}')
