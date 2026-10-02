"""A very small XLSX reader/writer built on the standard library only.

An ``.xlsx`` file is just a ZIP archive of XML parts, so ``zipfile`` plus
``xml.etree`` is enough to produce files that Excel, LibreOffice and
Translator++ all open happily. This exists so the glossary tool can export
spreadsheets without pulling in ``openpyxl``.

Only what a glossary needs is implemented: a single sheet, all cells stored as
inline strings, a bold header row and plain data rows.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Iterable, Sequence
from xml.etree import ElementTree

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="{_REL_NS}/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="{_MAIN_NS}">
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def col_letter(index: int) -> str:
    """0 -> ``A``, 25 -> ``Z``, 26 -> ``AA``."""
    letters = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _clean_xml_text(text: str) -> str:
    """Strip characters that are illegal in XML 1.0."""
    return "".join(ch for ch in text if ch in "\t\n\r" or 0x20 <= ord(ch) <= 0xD7FF or 0xE000 <= ord(ch) <= 0xFFFD)


def _inline_cell(ref: str, value: str, bold: bool) -> str:
    text = _escape(_clean_xml_text("" if value is None else str(value)))
    style = ' s="1"' if bold else ""
    space = ' xml:space="preserve"' if text != text.strip() else ""
    return f'<c r="{ref}" t="inlineStr"{style}><is><t{space}>{text}</t></is></c>'


def write_xlsx(
    path: str | Path,
    rows: Sequence[Sequence[str]],
    sheet_name: str = "Glossary",
    header: bool = True,
) -> Path:
    """Write ``rows`` to ``path`` as a single-sheet inline-string workbook."""
    path = Path(path)
    sheet_name = (sheet_name or "Sheet1")[:31].replace("/", "-").replace("\\", "-").replace("[", "").replace("]", "").replace(":", "")

    body: list[str] = []
    for row_index, row in enumerate(rows, start=1):
        cells = "".join(
            _inline_cell(f"{col_letter(col_index)}{row_index}", value, header and row_index == 1)
            for col_index, value in enumerate(row)
        )
        body.append(f'<row r="{row_index}">{cells}</row>')

    worksheet = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<worksheet xmlns="{_MAIN_NS}">'
        f'<sheetData>{"".join(body)}</sheetData>'
        "</worksheet>"
    )
    workbook = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_MAIN_NS}" xmlns:r="{_REL_NS}">'
        f'<sheets><sheet name="{_escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{_REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>'
        f'<Relationship Id="rId2" Type="{_REL_NS}/styles" Target="styles.xml"/>'
        "</Relationships>"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
        archive.writestr("_rels/.rels", _ROOT_RELS)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", _STYLES)
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)
    return path


def read_xlsx(path: str | Path, max_rows: int | None = None) -> list[list[str]]:
    """Read the first worksheet of ``path`` into a list of row lists.

    Handles inline strings, shared strings, formula results and plain numbers.
    """
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall(f"{{{_MAIN_NS}}}si"):
                shared.append("".join(node.text or "" for node in item.iter(f"{{{_MAIN_NS}}}t")))

        sheet_name = "xl/worksheets/sheet1.xml"
        names = archive.namelist()
        if sheet_name not in names:
            candidates = sorted(n for n in names if n.startswith("xl/worksheets/") and n.endswith(".xml"))
            if not candidates:
                raise ValueError(f"{path.name} 中没有找到工作表。")
            sheet_name = candidates[0]

        root = ElementTree.fromstring(archive.read(sheet_name))

    rows: list[list[str]] = []
    sheet_data = root.find(f"{{{_MAIN_NS}}}sheetData")
    if sheet_data is None:
        return rows

    for row_node in sheet_data.findall(f"{{{_MAIN_NS}}}row"):
        values: dict[int, str] = {}
        for cell in row_node.findall(f"{{{_MAIN_NS}}}c"):
            reference = cell.get("r") or ""
            column = 0
            for char in reference:
                if char.isalpha():
                    column = column * 26 + (ord(char.upper()) - 64)
                else:
                    break
            column -= 1
            cell_type = cell.get("t")
            if cell_type == "inlineStr":
                text_node = cell.find(f"{{{_MAIN_NS}}}is/{{{_MAIN_NS}}}t")
                value = text_node.text if text_node is not None and text_node.text else ""
            elif cell_type == "s":
                node = cell.find(f"{{{_MAIN_NS}}}v")
                try:
                    value = shared[int(node.text)] if node is not None and node.text else ""
                except (ValueError, IndexError):
                    value = ""
            else:
                node = cell.find(f"{{{_MAIN_NS}}}v")
                value = node.text if node is not None and node.text is not None else ""
                if cell_type == "b":
                    value = "TRUE" if value.strip() in {"1", "true", "TRUE"} else "FALSE"
            values[max(column, 0)] = value
        width = max(values) + 1 if values else 0
        rows.append([values.get(i, "") for i in range(width)])
        if max_rows is not None and len(rows) >= max_rows:
            break
    return rows


def iter_rows(rows: Iterable[Sequence[str]]) -> Iterable[tuple[int, Sequence[str]]]:
    for index, row in enumerate(rows, start=1):
        yield index, row