"""PDF edits appended to a copy, with fail-closed metadata verification."""
from dataclasses import dataclass
from pathlib import Path
import os
import tempfile
import pymupdf as pdf


@dataclass(frozen=True)
class Edit:
    page: int
    tool: str
    rect: tuple
    color: tuple = (0.08, 0.12, 0.2)
    text: str = ""
    size: float = 18
    image: bytes = b""


def metadata_snapshot(doc):
    """Include custom Info entries and indirect values, plus every XMP stream."""
    objects = {}
    def collect(xref):
        if xref in objects:
            return
        objects[xref] = (doc.xref_object(xref),
                         doc.xref_stream(xref) if doc.xref_is_stream(xref) else None)
        import re
        for ref in re.findall(r'(?<!\d)(\d+)\s+0\s+R\b', objects[xref][0]):
            collect(int(ref))
    kind, value = doc.xref_get_key(-1, "Info")
    if kind == "xref":
        collect(int(value.split()[0]))
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Type") == ("name", "/Metadata"):
            collect(xref)
    return (dict(doc.metadata), (kind, value), doc.xref_get_key(-1, "ID"), objects)


def apply_edit(doc, edit):
    page = doc[edit.page]
    rect = pdf.Rect(edit.rect) * page.derotation_matrix
    if rect.is_empty or rect.is_infinite:
        raise ValueError("Draw a larger area inside the page.")
    rotation = (360 - page.rotation) % 360
    if edit.tool == "Text":
        # Standard Helvetica cannot faithfully represent arbitrary Unicode.
        try:
            edit.text.encode("cp1252")
        except UnicodeEncodeError:
            raise ValueError("Text currently supports Western European characters only.")
        result = page.insert_textbox(rect, edit.text, fontsize=edit.size,
                                     fontname="helv", color=edit.color, rotate=rotation)
        if result < 0:
            raise ValueError("Text does not fit. Draw a larger box or reduce the font size.")
    elif edit.tool == "Image":
        page.insert_image(rect, stream=edit.image, keep_proportion=True, rotate=rotation)
    elif edit.tool == "Cover":
        page.draw_rect(rect, color=None, fill=(1, 1, 1), overlay=True)
    elif edit.tool == "Highlight":
        page.draw_rect(rect, color=None, fill=edit.color, fill_opacity=0.28, overlay=True)
    elif edit.tool == "Rectangle":
        page.draw_rect(rect, color=edit.color, width=2, overlay=True)
    else:
        raise ValueError("Unknown editing tool")


class Document:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.data = self.path.read_bytes()
        with pdf.open(stream=self.data, filetype="pdf") as doc:
            if doc.is_encrypted:
                raise ValueError("Password-protected PDFs are not supported in this version.")
            if not doc.is_pdf or not doc.page_count or doc.is_repaired:
                raise ValueError("Open an intact PDF with at least one page.")
            if doc.get_sigflags() > 0:
                raise ValueError("Signed PDFs are not supported: visual edits may affect signatures.")
            self.metadata = metadata_snapshot(doc)
            self.pages = doc.page_count
        self.edits = []
        self.redo_stack = []

    def render(self, page_number, scale=1.25):
        with pdf.open(stream=self.data, filetype="pdf") as doc:
            for edit in self.edits:
                if edit.page == page_number:
                    apply_edit(doc, edit)
            return doc[page_number].get_pixmap(matrix=pdf.Matrix(scale, scale), alpha=False)

    def add(self, edit):
        with pdf.open(stream=self.data, filetype="pdf") as doc:
            apply_edit(doc, edit)
        self.edits.append(edit)
        self.redo_stack.clear()

    def undo(self):
        if self.edits:
            self.redo_stack.append(self.edits.pop())

    def redo(self):
        if self.redo_stack:
            self.edits.append(self.redo_stack.pop())

    def export(self, destination):
        destination = Path(destination).resolve()
        if destination == self.path or (destination.exists() and self.path.exists()
                                        and os.path.samefile(destination, self.path)):
            raise ValueError("Choose a different filename to keep the original intact.")
        fd, temporary = tempfile.mkstemp(suffix=".pdf", dir=destination.parent)
        os.close(fd)
        try:
            Path(temporary).write_bytes(self.data)
            with pdf.open(temporary) as doc:
                for edit in self.edits:
                    apply_edit(doc, edit)
                if self.edits:
                    if not doc.can_save_incrementally():
                        raise ValueError("This PDF cannot be saved while retaining the original structure.")
                    doc.save(temporary, incremental=True, no_new_id=True,
                             encryption=pdf.PDF_ENCRYPT_KEEP)
            with pdf.open(temporary) as result:
                if metadata_snapshot(result) != self.metadata:
                    raise ValueError("Metadata verification failed. No output was saved.")
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
