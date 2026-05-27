from __future__ import annotations

import csv
import importlib
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any


class FileLoader:
    """Load CSV / Excel rows from either a filesystem path or uploaded bytes."""

    def load_rows(self, file_path: str) -> list[list[Any]]:
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")
        return self.load_rows_from_bytes(path.name, path.read_bytes())

    def load_rows_from_bytes(self, file_name: str, content: bytes) -> list[list[Any]]:
        suffix = Path(file_name).suffix.lower()
        if suffix == ".csv":
            return self._load_csv(content)
        if suffix in {".xlsx", ".xlsm"}:
            return self._load_workbook(content)
        if suffix == ".xls":
            return self._load_legacy_xls(content)
        raise ValueError(f"Unsupported file type: {suffix or '(none)'}")

    @staticmethod
    def _load_csv(content: bytes) -> list[list[Any]]:
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                text = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = content.decode("utf-8", errors="replace")
        return [list(row) for row in csv.reader(StringIO(text))]

    @staticmethod
    def _load_workbook(content: bytes) -> list[list[Any]]:
        try:
            openpyxl_module = importlib.import_module("openpyxl")
        except ImportError as exc:
            raise RuntimeError("Reading .xlsx/.xlsm files requires openpyxl.") from exc
        load_workbook = getattr(openpyxl_module, "load_workbook")
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
        try:
            sheet = workbook.active
            if sheet is None:
                return []
            return [list(row) for row in sheet.iter_rows(values_only=True)]
        finally:
            workbook.close()

    @staticmethod
    def _load_legacy_xls(content: bytes) -> list[list[Any]]:
        try:
            xlrd = importlib.import_module("xlrd")
        except ImportError as exc:
            raise RuntimeError("Reading .xls files requires xlrd==1.2.0.") from exc

        book = xlrd.open_workbook(file_contents=content)
        sheet = book.sheet_by_index(0)
        rows: list[list[Any]] = []
        for r in range(sheet.nrows):
            row: list[Any] = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                val: Any = cell.value
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        tup = xlrd.xldate_as_tuple(val, book.datemode)
                        if tup[3:] == (0, 0, 0):
                            val = f"{tup[0]:04d}-{tup[1]:02d}-{tup[2]:02d}"
                        else:
                            val = (
                                f"{tup[0]:04d}-{tup[1]:02d}-{tup[2]:02d} "
                                f"{tup[3]:02d}:{tup[4]:02d}:{tup[5]:02d}"
                            )
                    except (ValueError, OverflowError):
                        pass
                elif cell.ctype == xlrd.XL_CELL_NUMBER and float(val).is_integer():
                    val = int(val)
                elif cell.ctype == xlrd.XL_CELL_EMPTY:
                    val = ""
                row.append(val)
            rows.append(row)
        return rows
