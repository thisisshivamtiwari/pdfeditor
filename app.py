"""Local desktop PDF editor. Run with .venv/bin/python app.py."""
import sys
from pathlib import Path
from PySide6.QtCore import Qt, QRect, Signal
from PySide6.QtGui import QColor, QImage, QPixmap, QPainter, QPen, QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QLabel, QPushButton,
    QVBoxLayout, QHBoxLayout, QScrollArea, QFileDialog, QMessageBox, QComboBox,
    QSpinBox, QColorDialog, QPlainTextEdit, QSplitter, QFrame)
from pdf_engine import Document, Edit


class PageCanvas(QLabel):
    selected = Signal(tuple)
    def __init__(self):
        super().__init__()
        self.start = self.end = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.pixmap():
            self.start = self.end = event.position().toPoint()

    def mouseMoveEvent(self, event):
        if self.start is not None:
            self.end = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event):
        if self.start is not None:
            rect = QRect(self.start, event.position().toPoint()).normalized().intersected(self.rect())
            self.start = self.end = None
            self.update()
            if rect.width() > 4 and rect.height() > 4:
                self.selected.emit((rect.left(), rect.top(), rect.right(), rect.bottom()))

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.start is not None:
            painter = QPainter(self)
            painter.setPen(QPen(QColor('#6366f1'), 2, Qt.PenStyle.DashLine))
            painter.setBrush(QColor(99, 102, 241, 35))
            painter.drawRect(QRect(self.start, self.end).normalized())


class Editor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.document = None
        self.page = 0
        self.scale = 1.25
        self.color = (0.1, 0.2, 0.65)
        self.image_data = b''
        self.saved_edits = []
        self.setWindowTitle('Still • PDF editor')
        self.resize(1180, 820)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 12)
        header = QHBoxLayout()
        brand = QLabel('Still  /  PDF editor')
        brand.setStyleSheet('font-size: 23px; font-weight: 700;')
        header.addWidget(brand)
        header.addStretch()
        self.button(header, 'Open PDF', self.open_file)
        self.save_button = self.button(header, 'Save edited copy', self.save_file)
        layout.addLayout(header)
        self.file_label = QLabel('Your documents stay on this computer.')
        layout.addWidget(self.file_label)
        split = QSplitter()
        layout.addWidget(split, 1)
        side = QWidget()
        sidebar = QVBoxLayout(side)
        sidebar.setContentsMargins(0, 18, 18, 10)
        sidebar.addWidget(QLabel('EDIT THE PAGE'))
        self.tool = QComboBox()
        self.tool.addItems(['Text', 'Image', 'Rectangle', 'Highlight', 'Cover'])
        sidebar.addWidget(self.tool)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText('Enter text, then drag a box on the page…')
        self.text.setMaximumHeight(140)
        sidebar.addWidget(self.text)
        self.size = QSpinBox()
        self.size.setRange(6, 144)
        self.size.setValue(18)
        self.size.setSuffix(' pt')
        sidebar.addWidget(self.size)
        self.button(sidebar, 'Choose color', self.pick_color)
        self.image_button = self.button(sidebar, 'Choose image…', self.pick_image)
        hint = QLabel('Drag a box to place your edit.\n\nTo visually replace text, cover the old text, then add new text above it.\n\nCover is not secure redaction: the original content remains recoverable.')
        hint.setWordWrap(True)
        hint.setStyleSheet('color: #6b7280; font-size: 12px;')
        sidebar.addWidget(hint)
        row = QHBoxLayout()
        self.undo_button = self.button(row, 'Undo', self.undo)
        self.redo_button = self.button(row, 'Redo', self.redo)
        sidebar.addLayout(row)
        sidebar.addSpacing(20)
        sidebar.addWidget(QLabel('ORIGINAL METADATA'))
        self.meta = QPlainTextEdit()
        self.meta.setReadOnly(True)
        self.meta.setPlaceholderText('Open a PDF to inspect its metadata.')
        sidebar.addWidget(self.meta, 1)
        side.setMinimumWidth(245)
        side.setMaximumWidth(300)
        split.addWidget(side)
        self.scroll = QScrollArea()
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setStyleSheet('QScrollArea { background: #e7e9ef; border: 0; border-radius: 8px; }')
        self.canvas = PageCanvas()
        self.canvas.setText('Open a PDF to start editing\n\nText · Images · Shapes · Highlights')
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.resize(640, 600)
        self.scroll.setWidget(self.canvas)
        split.addWidget(self.scroll)
        self.canvas.selected.connect(self.add_edit)
        footer = QHBoxLayout()
        self.previous_button = self.button(footer, '← Previous', lambda: self.navigate(-1))
        self.page_label = QLabel('No document')
        footer.addWidget(self.page_label)
        self.next_button = self.button(footer, 'Next →', lambda: self.navigate(1))
        footer.addStretch()
        self.zoom = QComboBox()
        self.zoom.addItems(['75%', '100%', '125%', '150%', '200%'])
        self.zoom.setCurrentText('125%')
        self.zoom.currentTextChanged.connect(self.change_zoom)
        footer.addWidget(self.zoom)
        layout.addLayout(footer)
        self.statusBar().showMessage('Metadata is checked before every export.')
        for key, callback in [('Open', self.open_file), ('Save', self.save_file), ('Undo', self.undo), ('Redo', self.redo)]:
            action = QAction(self)
            action.setShortcut(getattr(QKeySequence.StandardKey, key))
            action.triggered.connect(callback)
            self.addAction(action)
        self.update_controls()

    @staticmethod
    def button(layout, text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    def error(self, error):
        QMessageBox.warning(self, 'Could not complete the edit', str(error))

    def can_leave(self):
        if self.document and self.document.edits != self.saved_edits:
            return QMessageBox.question(self, 'Unsaved edits', 'Discard unsaved edits?',
                QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel) == QMessageBox.StandardButton.Discard
        return True

    def open_file(self):
        if not self.can_leave():
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Open PDF', '', 'PDF files (*.pdf)')
        if path:
            self.load(path)

    def load(self, path):
        try:
            document = Document(path)
            document.render(0)  # Check rendering before replacing the active document.
            self.document = document
            self.page = 0
            self.saved_edits = []
            self.file_label.setText(Path(path).name)
            values = document.metadata[0]
            self.meta.setPlainText('\n\n'.join(f'{k}: {v or "—"}' for k, v in values.items()) +
                '\n\nCustom Info entries, XMP streams and document IDs are also preserved and verified.')
            self.refresh()
        except Exception as error:
            self.error(error)

    def refresh(self):
        if self.document:
            pix = self.document.render(self.page, self.scale)
            image = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
            self.canvas.setPixmap(QPixmap.fromImage(image))
            self.canvas.resize(pix.width, pix.height)
            self.page_label.setText(f'Page {self.page + 1} of {self.document.pages}')
            self.statusBar().showMessage(f'{len(self.document.edits)} edits • Original PDF metadata retained')
        self.update_controls()

    def update_controls(self):
        enabled = self.document is not None
        self.save_button.setEnabled(enabled)
        self.undo_button.setEnabled(enabled and bool(self.document.edits))
        self.redo_button.setEnabled(enabled and bool(self.document.redo_stack))
        self.previous_button.setEnabled(enabled and self.page > 0)
        self.next_button.setEnabled(enabled and self.page < self.document.pages - 1)

    def pick_color(self):
        color = QColorDialog.getColor(QColor.fromRgbF(*self.color), self)
        if color.isValid():
            self.color = (color.redF(), color.greenF(), color.blueF())

    def pick_image(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose image', '', 'Images (*.png *.jpg *.jpeg)')
        if path:
            try:
                self.image_data = Path(path).read_bytes()
                self.image_button.setText(Path(path).name)
                self.tool.setCurrentText('Image')
            except OSError as error:
                self.error(error)

    def add_edit(self, rect):
        if not self.document:
            return
        try:
            tool = self.tool.currentText()
            if tool == 'Text' and not self.text.toPlainText().strip():
                raise ValueError('Enter the text in the sidebar first.')
            if tool == 'Image' and not self.image_data:
                raise ValueError('Choose an image first.')
            edit = Edit(self.page, tool, tuple(v / self.scale for v in rect), self.color,
                        self.text.toPlainText(), self.size.value(), self.image_data)
            self.document.add(edit)
            self.refresh()
        except Exception as error:
            self.error(error)

    def undo(self):
        if self.document:
            self.document.undo()
            self.refresh()

    def redo(self):
        if self.document:
            self.document.redo()
            self.refresh()

    def navigate(self, direction):
        if self.document:
            self.page = max(0, min(self.document.pages - 1, self.page + direction))
            self.refresh()

    def change_zoom(self, value):
        self.scale = int(value.rstrip('%')) / 100
        self.refresh()

    def save_file(self):
        if not self.document:
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Save edited copy',
            str(self.document.path.with_stem(self.document.path.stem + '-edited')), 'PDF files (*.pdf)')
        if path:
            if not path.lower().endswith('.pdf'):
                path += '.pdf'
                if Path(path).exists() and QMessageBox.question(self, 'Replace file?',
                    f'Replace {Path(path).name}?') != QMessageBox.StandardButton.Yes:
                    return
            try:
                self.document.export(path)
                self.saved_edits = list(self.document.edits)
                self.statusBar().showMessage('Saved • Metadata verification passed • ' + path)
            except Exception as error:
                self.error(error)

    def closeEvent(self, event):
        event.accept() if self.can_leave() else event.ignore()


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet('''
        QWidget { font-family: ".AppleSystemUIFont", "Segoe UI", sans-serif; font-size: 13px; color: #182135; }
        QMainWindow, QSplitter, QWidget#central { background: #fafbfe; }
        QPushButton { padding: 9px 12px; border: 1px solid #d4d9e3; border-radius: 6px; background: white; }
        QPushButton:hover { background: #eef0ff; border-color: #818cf8; }
        QPushButton:disabled { color: #9ca3af; background: #f3f4f6; }
        QPlainTextEdit, QComboBox, QSpinBox { background: white; border: 1px solid #d4d9e3; border-radius: 5px; padding: 7px; }
    ''')
    window = Editor()
    window.show()
    if len(sys.argv) > 1:
        window.load(sys.argv[1])
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
