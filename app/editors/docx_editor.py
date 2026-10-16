"""edit_docx: replace_text and append_blocks. Always returns new bytes; the source is never modified.

replace_text works paragraph by paragraph over the document body, headers, and footers (text boxes
included). A match that spans several runs is written into the run where it starts, keeping that run's
formatting; the other runs lose only the matched characters. Matches never cross a tab, a line break, or a
drawing. Tracked deletions are not searched. Matches inside mc:Fallback copies are replaced but not counted.
"""

import io
import re

from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn

from app.builders.docx_builder import DocxRenderer, Images
from app.core.exceptions import corrupt_file
from app.readers.docx_reader import MC_FALLBACK, open_document
from app.schemas.docx_spec import AppendBlocksOp, ReplaceTextOp

W_P, W_T = qn("w:p"), qn("w:t")
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_BARRIERS = {
    qn(t)
    for t in ("w:tab", "w:br", "w:cr", "w:sym", "w:ptab", "w:drawing", "w:pict", "w:object", "w:fldChar")
    + ("w:footnoteReference", "w:endnoteReference")
}
_NOT_SEARCHED = {qn("w:del"), qn("w:moveFrom"), qn("w:pPr"), qn("w:rPr"), W_P}


def _segments(p) -> tuple[list, str]:
    """w:t elements of this paragraph (not of nested text-box paragraphs) and the text with barriers as NUL."""
    segs: list = []
    text: list[str] = []

    def walk(el) -> None:
        for child in el:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag == W_T:
                segs.append((child, sum(len(t) for t in text)))
                text.append(child.text or "")
            elif tag in _BARRIERS:
                text.append("\x00")
            elif tag not in _NOT_SEARCHED:
                walk(child)

    walk(p)
    return segs, "".join(text)


def _in_fallback(el) -> bool:
    return any(a.tag == MC_FALLBACK for a in el.iterancestors())


def _replace_in_paragraph(p, pattern: re.Pattern, replacement: str) -> int:
    segs, full = _segments(p)
    matches = list(pattern.finditer(full))
    if not matches:
        return 0
    texts = [t.text or "" for t, _ in segs]
    starts = [pos for _, pos in segs]

    def seg_at(pos: int) -> int:
        for i, start in enumerate(starts):
            if start <= pos < start + len(segs[i][0].text or ""):
                return i
        return -1

    count = 0
    for m in reversed(matches):
        s, e = m.span()
        i, j = seg_at(s), seg_at(e - 1)
        if i < 0 or j < 0:
            continue
        ls, le = s - starts[i], e - starts[j]
        if i == j:
            texts[i] = texts[i][:ls] + replacement + texts[i][le:]
        else:
            texts[i] = texts[i][:ls] + replacement
            for k in range(i + 1, j):
                texts[k] = ""
            texts[j] = texts[j][le:]
        count += 1
    for (t, _), new in zip(segs, texts, strict=True):
        if (t.text or "") != new:
            t.text = new
            if new != new.strip():
                t.set(XML_SPACE, "preserve")
    return count


def _story_roots(doc) -> list:
    roots = [doc.element]
    seen = set()
    for rel in doc.part.rels.values():
        if rel.is_external or rel.reltype not in (RT.HEADER, RT.FOOTER):
            continue
        part = rel.target_part
        if id(part) not in seen:
            seen.add(id(part))
            roots.append(part.element)
    return roots


def _replace_text(doc, op: ReplaceTextOp) -> int:
    pattern = re.compile(re.escape(op.find), 0 if op.match_case else re.IGNORECASE)
    total = 0
    for root in _story_roots(doc):
        for p in list(root.iter(W_P)):
            n = _replace_in_paragraph(p, pattern, op.replace)
            if not _in_fallback(p):
                total += n
    return total


def edit_docx(data: bytes, operations: list, images: Images) -> tuple[bytes, list[dict]]:
    doc = open_document(data)
    results: list[dict] = []
    renderer = DocxRenderer(doc, images)
    try:
        for op in operations:
            if isinstance(op, ReplaceTextOp):
                results.append({"type": "replace_text", "replacements": _replace_text(doc, op)})
            elif isinstance(op, AppendBlocksOp):
                renderer.render(op.blocks)
                results.append({"type": "append_blocks", "blocks_added": len(op.blocks)})
        out = io.BytesIO()
        doc.save(out)
    except Exception:
        raise corrupt_file() from None
    return out.getvalue(), results
