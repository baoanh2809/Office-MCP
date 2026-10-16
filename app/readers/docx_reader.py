"""read_docx: body → ordered blocks (heading, paragraph, list, table).

Text comes straight from the XML so that content python-docx skips is handled on purpose: hyperlinks,
content controls, and tracked insertions are read; text boxes, tracked deletions, and mc:Fallback copies are
not. Headers, footers, footnotes, and comments are not returned.
"""

import io
import re
from dataclasses import dataclass, field

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn

from app.core import limits
from app.core.budget import TextBudget
from app.core.exceptions import corrupt_file

MC_FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
W_P, W_TBL, W_TR, W_TC, W_T = qn("w:p"), qn("w:tbl"), qn("w:tr"), qn("w:tc"), qn("w:t")
W_TAB, W_BR, W_CR = qn("w:tab"), qn("w:br"), qn("w:cr")
W_SDT, W_SDT_CONTENT = qn("w:sdt"), qn("w:sdtContent")
_TRANSPARENT = {qn("w:customXml"), qn("w:ins"), qn("w:moveTo"), qn("w:smartTag")}
_SKIP_IN_PARAGRAPH = {
    qn("w:pPr"),
    qn("w:rPr"),
    qn("w:txbxContent"),
    qn("w:del"),
    qn("w:moveFrom"),
    qn("w:delText"),
    qn("w:instrText"),
    MC_FALLBACK,
}
_HEADING_NAME = re.compile(r"^heading ([1-9])$")


def paragraph_text(p) -> str:
    out: list[str] = []

    def walk(el) -> None:
        for child in el:
            tag = child.tag
            if tag == W_T:
                out.append(child.text or "")
            elif tag == W_TAB:
                out.append("\t")
            elif tag in (W_BR, W_CR):
                if child.get(qn("w:type")) not in ("page", "column"):
                    out.append("\n")
            elif tag not in _SKIP_IN_PARAGRAPH and isinstance(tag, str):
                walk(child)

    walk(p)
    return "".join(out)


def _table_rows(tbl) -> list[list[str]]:
    rows = []
    for tr in tbl.iter(W_TR):
        if tr.getparent() is not tbl and tr.getparent().tag != W_SDT_CONTENT:
            continue  # rows of nested tables are flattened into their cell
        row = []
        for tc in tr.iter(W_TC):
            if tc.getparent() is not tr:
                continue
            parts = []
            for child in tc:
                if child.tag == W_P:
                    parts.append(paragraph_text(child))
                elif child.tag == W_TBL:
                    parts.extend(" | ".join(r) for r in _table_rows(child))
            row.append("\n".join(p for p in parts if p))
        rows.append(row)
    return rows


@dataclass
class DocxReadResult:
    blocks: list[dict] = field(default_factory=list)
    char_count: int = 0
    truncated: bool = False


class _Collector:
    def __init__(self, doc, budget: TextBudget) -> None:
        self.budget = budget
        self.blocks: list[dict] = []
        self._list_num: str | None = None
        self.styles: dict[str, tuple[str, str | None]] = {}
        for s in doc.styles.element.findall(qn("w:style")):
            name_el = s.find(qn("w:name"))
            num_el = s.find(f"{qn('w:pPr')}/{qn('w:numPr')}/{qn('w:numId')}")
            self.styles[s.get(qn("w:styleId"))] = (
                (name_el.get(qn("w:val")) if name_el is not None else "").lower(),
                num_el.get(qn("w:val")) if num_el is not None else None,
            )
        self.num_formats: dict[str, dict[str, str]] = {}
        try:
            numbering = doc.part.part_related_by(RT.NUMBERING).element
        except KeyError:
            numbering = None
        if numbering is not None:
            abstract = {
                a.get(qn("w:abstractNumId")): {
                    lvl.get(qn("w:ilvl")): (
                        lvl.find(qn("w:numFmt")).get(qn("w:val")) if lvl.find(qn("w:numFmt")) is not None else ""
                    )
                    for lvl in a.findall(qn("w:lvl"))
                }
                for a in numbering.findall(qn("w:abstractNum"))
            }
            for n in numbering.findall(qn("w:num")):
                ref = n.find(qn("w:abstractNumId"))
                if ref is not None:
                    self.num_formats[n.get(qn("w:numId"))] = abstract.get(ref.get(qn("w:val")), {})

    def _heading_level(self, style_id: str | None) -> int:
        name = self.styles.get(style_id or "", ("", None))[0]
        if m := _HEADING_NAME.match(name):
            return int(m.group(1))
        return 1 if name == "title" else 0

    def _ordered(self, num_id: str, ilvl: str, style_id: str | None) -> bool:
        fmt = self.num_formats.get(num_id, {}).get(ilvl or "0")
        if fmt is not None:
            return fmt not in ("bullet", "none", "")
        return "number" in self.styles.get(style_id or "", ("", None))[0]

    def paragraph(self, p) -> None:
        text = paragraph_text(p)
        if not text.strip():
            return
        ppr = p.find(qn("w:pPr"))
        style_el = ppr.find(qn("w:pStyle")) if ppr is not None else None
        style_id = style_el.get(qn("w:val")) if style_el is not None else None
        num_el = ppr.find(f"{qn('w:numPr')}/{qn('w:numId')}") if ppr is not None else None
        ilvl_el = ppr.find(f"{qn('w:numPr')}/{qn('w:ilvl')}") if ppr is not None else None
        num_id = num_el.get(qn("w:val")) if num_el is not None else self.styles.get(style_id or "", ("", None))[1]
        if num_id and num_id != "0":
            taken = self.budget.take(text)
            if not taken:
                return
            if self._list_num == num_id and self.blocks and self.blocks[-1]["type"] == "list":
                self.blocks[-1]["items"].append(taken)
            else:
                ilvl = ilvl_el.get(qn("w:val")) if ilvl_el is not None else "0"
                ordered = self._ordered(num_id, ilvl, style_id)
                self.blocks.append({"type": "list", "ordered": ordered, "items": [taken]})
                self._list_num = num_id
            return
        self._list_num = None
        taken = self.budget.take(text)
        if not taken:
            return
        if level := self._heading_level(style_id):
            self.blocks.append({"type": "heading", "level": level, "text": taken})
        else:
            self.blocks.append({"type": "paragraph", "text": taken})

    def table(self, tbl) -> None:
        self._list_num = None
        rows = []
        for row in _table_rows(tbl):
            if self.budget.exhausted:
                self.budget.truncated = True
                break
            cells = []
            for cell in row:
                if self.budget.exhausted and cell:
                    self.budget.truncated = True
                    break
                cells.append(self.budget.take(cell))
            rows.append(cells)
        if rows:
            self.blocks.append({"type": "table", "rows": rows})

    def walk(self, container) -> None:
        for child in container:
            if self.budget.truncated:
                return
            if child.tag == W_P:
                self.paragraph(child)
            elif child.tag == W_TBL:
                self.table(child)
            elif child.tag == W_SDT:
                content = child.find(W_SDT_CONTENT)
                if content is not None:
                    self.walk(content)
            elif child.tag in _TRANSPARENT:
                self.walk(child)


def open_document(data: bytes):
    try:
        return Document(io.BytesIO(data))
    except Exception:
        raise corrupt_file() from None


def read_docx(data: bytes, max_chars: int | None = None) -> DocxReadResult:
    doc = open_document(data)
    budget = TextBudget(limits.clamp(max_chars, limits.DEFAULT_MAX_CHARS, limits.MAX_MAX_CHARS))
    try:
        collector = _Collector(doc, budget)
        collector.walk(doc.element.body)
    except Exception:
        raise corrupt_file() from None
    return DocxReadResult(blocks=collector.blocks, char_count=budget.used, truncated=budget.truncated)
