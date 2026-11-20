"""CSV, XLSX and ODS readers."""

import csv
import zipfile
import xml.etree.ElementTree as ET

from pathlib import Path

from .normalizers import clean


def read_csv(path):
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)

        if not reader.fieldnames:
            raise ValueError("CSV has no header row")

        headers = [clean(x) for x in reader.fieldnames]

        for row_number, row in enumerate(reader, 2):
            yield row_number, {
                headers[index]: clean(value)
                for index, value in enumerate(row.values())
                if index < len(headers)
            }


def read_xlsx(path):
    import openpyxl

    workbook = openpyxl.load_workbook(
        path,
        read_only=True,
        data_only=True,
    )

    try:
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)

        headers = next(iterator, None)

        if not headers:
            raise ValueError("XLSX has no header row")

        headers = [clean(x) for x in headers]

        for row_number, values in enumerate(iterator, 2):
            if not any(value is not None for value in values):
                continue

            yield row_number, {
                headers[index]: clean(value)
                for index, value in enumerate(values)
                if index < len(headers) and headers[index]
            }

    finally:
        workbook.close()


NS = {
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}


def read_ods(path):
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("content.xml"))

    sheets = root.findall(".//table:table", NS)

    if not sheets:
        raise ValueError("ODS contains no worksheets")

    sheet = sheets[0]
    rows = sheet.findall("table:table-row", NS)

    headers = None

    for row_number, row in enumerate(rows, 1):
        values = []

        for cell in row:
            tag = cell.tag.split("}")[-1]

            if tag not in ("table-cell", "covered-table-cell"):
                continue

            paragraphs = cell.findall(".//text:p", NS)

            value = " ".join(
                "".join(p.itertext()).strip()
                for p in paragraphs
            ).strip()

            repeat = int(
                cell.get(
                    f"{{{NS['table']}}}number-columns-repeated",
                    "1",
                )
            )

            if repeat > 10000:
                raise ValueError("Unreasonable ODS column repetition")

            values.extend([value] * repeat)

        while values and not values[-1]:
            values.pop()

        if not values:
            continue

        if headers is None:
            headers = values
            continue

        yield row_number, {
            headers[index]: clean(value)
            for index, value in enumerate(values)
            if index < len(headers) and headers[index]
        }


def read_supplier_file(filename):
    path = Path(filename)

    if not path.is_file():
        raise FileNotFoundError(path)

    extension = path.suffix.lower()

    readers = {
        ".csv": read_csv,
        ".xlsx": read_xlsx,
        ".ods": read_ods,
    }

    if extension not in readers:
        raise ValueError(
            f"Unsupported file format: {extension}"
        )

    yield from readers[extension](path)
