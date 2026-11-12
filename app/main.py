import os
import sys
import traceback

from PySide6.QtCore import QSize, Qt, QThread, Signal, QRect, QPoint
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QColorDialog, QDialog, QFileDialog, QFormLayout, QGroupBox,
                               QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMainWindow, QMessageBox, QProgressBar, QPushButton, QRadioButton,
                               QRubberBand, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core  # noqa: E402
from core import fitz  # noqa: E402


def resource(rel):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, rel)


def pix_to_qpixmap(pix):
    fmt = QImage.Format.Format_RGB888 if pix.n == 3 else QImage.Format.Format_RGBA8888
    return QPixmap.fromImage(QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy())


class Worker(QThread):
    msg = Signal(str)
    ok = Signal(object)
    err = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            self.ok.emit(self.fn(self.msg.emit))
        except Exception as e:  # noqa
            traceback.print_exc()
            self.err.emit(f"{type(e).__name__}: {e}")


class Canvas(QLabel):
    """Sayfa görüntüsü üzerinde dikdörtgen seçtirir."""
    def __init__(self):
        super().__init__()
        self.band = QRubberBand(QRubberBand.Shape.Rectangle, self)
        self.origin = QPoint()
        self.sel = None

    def mousePressEvent(self, e):
        self.origin = e.position().toPoint()
        self.band.setGeometry(QRect(self.origin, QSize()))
        self.band.show()
        self.sel = None

    def mouseMoveEvent(self, e):
        self.band.setGeometry(QRect(self.origin, e.position().toPoint()).normalized())

    def mouseReleaseEvent(self, e):
        r = QRect(self.origin, e.position().toPoint()).normalized()
        self.sel = r if r.width() > 3 and r.height() > 3 else None


class PageEditor(QDialog):
    ZOOM = 1.5

    def __init__(self, win, pno):
        super().__init__(win)
        self.win, self.pno, self.img_path, self.color = win, pno, None, QColor(0, 0, 0)
        self.setWindowTitle(f"Sayfa {pno + 1} düzenle")
        self.resize(1100, 800)
        lay = QHBoxLayout(self)
        tools = QWidget()
        tools.setFixedWidth(260)
        tl = QVBoxLayout(tools)
        self.rb = {k: QRadioButton(t) for k, t in [("replace", "Yazıyı değiştir"), ("text", "Metin ekle"),
                                                      ("image", "Resim ekle"), ("erase", "Alanı sil")]}
        self.rb["replace"].setChecked(True)
        for r in self.rb.values():
            tl.addWidget(r)
        self.text = QLineEdit()
        self.text.setPlaceholderText("Yazı")
        btn_read = QPushButton("Seçili yazıyı oku")
        btn_read.clicked.connect(self.read_text)
        self.size = QSpinBox()
        self.size.setRange(4, 200)
        self.size.setValue(14)
        self.btn_color = QPushButton("Renk")
        self.btn_color.clicked.connect(self.pick_color)
        self.cover = QCheckBox("Eski yazının üstünü beyazla kapat")
        self.cover.setChecked(True)
        self.btn_img = QPushButton("Resim seç…")
        self.btn_img.clicked.connect(self.pick_img)
        self.all_pages = QCheckBox("Tüm sayfalara uygula (alan silme)")
        for w in (btn_read, self.text, QLabel("Yazı boyutu (ekleme)"), self.size, self.btn_color, self.cover,
                  self.btn_img, self.all_pages):
            tl.addWidget(w)
        tl.addWidget(QLabel("Sayfada fareyle bir alan çizin."))
        self.info = QLabel("")
        self.info.setWordWrap(True)
        tl.addWidget(self.info)
        ap = QPushButton("Uygula")
        ap.clicked.connect(self.apply)
        cl = QPushButton("Kapat")
        cl.clicked.connect(self.accept)
        tl.addWidget(ap)
        tl.addWidget(cl)
        tl.addStretch()
        self.canvas = Canvas()
        sa = QScrollArea()
        sa.setWidget(self.canvas)
        lay.addWidget(tools)
        lay.addWidget(sa, 1)
        self.render()

    def render(self):
        page = self.win.doc[self.pno]
        pix = page.get_pixmap(matrix=fitz.Matrix(self.ZOOM, self.ZOOM), alpha=False)
        self.canvas.setPixmap(pix_to_qpixmap(pix))
        self.canvas.resize(pix.width, pix.height)
        self.canvas.band.hide()
        self.canvas.sel = None

    def pdf_rect(self):
        s = self.canvas.sel
        if not s:
            return None
        z = self.ZOOM
        return fitz.Rect(s.left() / z, s.top() / z, s.right() / z, s.bottom() / z)

    def pick_color(self):
        c = QColorDialog.getColor(self.color, self)
        if c.isValid():
            self.color = c
            self.btn_color.setStyleSheet(f"background:{c.name()};")

    def pick_img(self):
        p, _ = QFileDialog.getOpenFileName(self, "Resim seç", "", "Resimler (*.png *.jpg *.jpeg)")
        if p:
            self.img_path = p
            self.btn_img.setText(os.path.basename(p))

    def read_text(self):
        r = self.pdf_rect()
        if not r:
            self.info.setText("Önce bir alan seçin.")
            return
        self.info.setText("Okunuyor…")
        QApplication.processEvents()
        try:
            self.text.setText(core.read_rect_text(self.win.doc[self.pno], r, self.win.ocr))
            self.info.setText("Okundu. Düzenleyip Uygula’ya basın.")
        except Exception as e:  # noqa
            self.info.setText(f"Hata: {e}")

    def apply(self):
        page = self.win.doc[self.pno]
        if page.rotation:
            self.info.setText("Döndürülmüş sayfada düzenleme yapılamaz; önce sayfayı 0° yapın.")
            return
        r = self.pdf_rect()
        if not r:
            self.info.setText("Önce bir alan seçin.")
            return
        c = (self.color.redF(), self.color.greenF(), self.color.blueF())
        try:
            if self.rb["replace"].isChecked():
                core.replace_rect_text(page, r, self.text.text(), self.cover.isChecked(), self.size.value(), c)
            elif self.rb["text"].isChecked():
                core.add_text(page, r, self.text.text(), self.size.value(), c)
            elif self.rb["image"].isChecked():
                if not self.img_path:
                    self.info.setText("Önce bir resim seçin.")
                    return
                core.add_image(page, r, self.img_path)
            else:
                pages = range(len(self.win.doc)) if self.all_pages.isChecked() else [self.pno]
                for p in pages:
                    core.apply_red(self.win.doc[p], [r], cover=True)
        except Exception as e:  # noqa
            self.info.setText(f"Hata: {e}")
            return
        self.win.modified = True
        self.render()
        self.info.setText("Uygulandı.")

    def done(self, r):
        self.win.refresh()
        super().done(r)


class WatermarkDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Filigran ekle")
        self.color = QColor(136, 136, 136)
        f = QFormLayout(self)
        self.text = QLineEdit("GİZLİ")
        self.size = QSpinBox()
        self.size.setRange(10, 400)
        self.size.setValue(90)
        self.op = QSlider(Qt.Orientation.Horizontal)
        self.op.setRange(5, 100)
        self.op.setValue(25)
        self.angle = QSpinBox()
        self.angle.setRange(-90, 90)
        self.angle.setValue(45)
        self.btn = QPushButton("Renk seç")
        self.btn.setStyleSheet("background:#888888;")
        self.btn.clicked.connect(self.pick)
        ok = QPushButton("Tüm sayfalara ekle")
        ok.clicked.connect(self.accept)
        for label, w in [("Yazı", self.text), ("Boyut", self.size), ("Renk", self.btn), ("Saydamlık", self.op),
                         ("Açı", self.angle)]:
            f.addRow(label, w)
        f.addRow(ok)

    def pick(self):
        c = QColorDialog.getColor(self.color, self)
        if c.isValid():
            self.color = c
            self.btn.setStyleSheet(f"background:{c.name()};")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PDF Studio")
        self.setWindowIcon(QIcon(resource("assets/icon.png")))
        self.resize(1200, 800)
        self.doc, self.path, self.modified, self.ocr, self._w = None, None, False, core.OCR(), None

        root = QWidget()
        self.setCentralWidget(root)
        lay = QHBoxLayout(root)

        side_scroll = QScrollArea()
        side_scroll.setFixedWidth(290)
        side_scroll.setWidgetResizable(True)
        side = QWidget()
        self.sl = QVBoxLayout(side)
        side_scroll.setWidget(side)

        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(QPixmap(resource("assets/icon.png")).scaled(36, 36, Qt.AspectRatioMode.KeepAspectRatio,
                                                                      Qt.TransformationMode.SmoothTransformation))
        title = QLabel("<b style='font-size:16px'>PDF Studio</b>")
        head.addWidget(logo)
        head.addWidget(title)
        head.addStretch()
        self.sl.addLayout(head)

        self.group("Dosya", [("PDF aç", self.open_pdf), ("Kaydet", self.save), ("Farklı kaydet", self.save_as),
                              ("PDF ekle (birleştir)", self.merge), ("Tümünü seç", self.select_all)])
        self.group("Sayfalar (seçili)", [("Sil", self.delete), ("90° döndür", self.rotate),
                                         ("← Sola taşı", lambda: self.move(-1)), ("Sağa taşı →", lambda: self.move(1)),
                                         ("Ayır (yeni PDF)", self.extract), ("Tek tek böl", self.split),
                                         ("Sayfayı düzenle", self.edit_page)])
        self.group("Dönüştür (seçili veya tümü)", [("Word", self.to_word), ("Excel", self.to_excel),
                                                    ("PowerPoint", self.to_ppt), ("PNG", lambda: self.to_img("png")),
                                                    ("JPG", lambda: self.to_img("jpg"))])
        self.force_ocr = QCheckBox("Metni OCR ile oku (zorla)")
        self.sl.addWidget(self.force_ocr)
        note = QLabel("Metni bozuk (anlamsız karakterli) PDF’lerde OCR otomatik devreye girer.")
        note.setWordWrap(True)
        note.setStyleSheet("color:#6A7775;font-size:11px;")
        self.sl.addWidget(note)
        self.group("Filigran", [("Ekle", self.wm_add), ("Kaldır", self.wm_remove)])
        self.group("Resimler (seçili sayfalar)", [("Resimleri çıkar", self.img_out), ("Resimleri sil", self.img_del)])
        self.sl.addStretch()

        self.list = QListWidget()
        self.list.setViewMode(QListView.ViewMode.IconMode)
        self.list.setIconSize(QSize(130, 175))
        self.list.setResizeMode(QListView.ResizeMode.Adjust)
        self.list.setMovement(QListView.Movement.Static)
        self.list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.list.setSpacing(10)
        self.list.itemDoubleClicked.connect(lambda it: self.edit_page(self.list.row(it)))

        lay.addWidget(side_scroll)
        lay.addWidget(self.list, 1)

        self.prog = QProgressBar()
        self.prog.setRange(0, 0)
        self.prog.setMaximumWidth(140)
        self.prog.hide()
        self.status = QLabel("Hazır.")
        self.statusBar().addWidget(self.status, 1)
        self.statusBar().addPermanentWidget(self.prog)

    # ---- yardımcılar
    def group(self, title, items):
        box = QGroupBox(title)
        v = QVBoxLayout(box)
        for text, fn in items:
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=fn: self.guard(f))
            v.addWidget(b)
        self.sl.addWidget(box)

    def guard(self, fn):
        try:
            fn()
        except Exception as e:  # noqa
            traceback.print_exc()
            self.say(f"Hata: {e}")

    def say(self, t):
        self.status.setText(t)

    def need_doc(self):
        if not self.doc:
            self.say("Önce bir PDF açın.")
            return False
        return True

    def sel_pages(self):
        rows = sorted(i.row() for i in self.list.selectedIndexes())
        return rows or list(range(len(self.doc)))

    def selected_only(self):
        return sorted(i.row() for i in self.list.selectedIndexes())

    def refresh(self, select=()):
        self.list.clear()
        for i in range(len(self.doc)):
            pix = self.doc[i].get_pixmap(matrix=fitz.Matrix(0.35, 0.35), alpha=False)
            it = QListWidgetItem(QIcon(pix_to_qpixmap(pix)), f"Sayfa {i + 1}")
            self.list.addItem(it)
            if i in select:
                it.setSelected(True)
        self.setWindowTitle(f"PDF Studio — {os.path.basename(self.path or 'adsız')} ({len(self.doc)} sayfa)")

    def run_bg(self, fn, done):
        self.setEnabled(False)
        self.prog.show()
        self._w = Worker(fn)
        self._w.msg.connect(self.say)

        def ok(r):
            self.setEnabled(True)
            self.prog.hide()
            done(r)

        def err(m):
            self.setEnabled(True)
            self.prog.hide()
            self.say("Hata: " + m)
            QMessageBox.warning(self, "Hata", m)

        self._w.ok.connect(ok)
        self._w.err.connect(err)
        self._w.start()

    # ---- dosya
    def open_pdf(self):
        p, _ = QFileDialog.getOpenFileName(self, "PDF aç", "", "PDF (*.pdf)")
        if not p:
            return
        self.doc, self.path, self.modified = fitz.open(p), p, False
        self.refresh()
        self.say(f"{len(self.doc)} sayfa açıldı.")

    def save_as(self):
        if not self.need_doc():
            return
        base = os.path.splitext(self.path or "belge")[0] + "-duzenlendi.pdf"
        p, _ = QFileDialog.getSaveFileName(self, "Kaydet", base, "PDF (*.pdf)")
        if not p:
            return
        self.doc.save(p, garbage=3, deflate=True)
        self.say("Kaydedildi: " + p)

    def save(self):
        self.save_as()

    def merge(self):
        if not self.need_doc():
            return
        ps, _ = QFileDialog.getOpenFileNames(self, "Eklenecek PDF'ler", "", "PDF (*.pdf)")
        for p in ps:
            o = fitz.open(p)
            self.doc.insert_pdf(o)
            o.close()
        if ps:
            self.refresh()
            self.say("Birleştirildi.")

    def select_all(self):
        if self.need_doc():
            self.list.selectAll()

    # ---- sayfalar
    def delete(self):
        if not self.need_doc():
            return
        s = self.selected_only()
        if not s or len(s) >= len(self.doc):
            self.say("Silmek için bir veya daha fazla sayfa seçin (hepsi silinemez).")
            return
        self.doc.delete_pages(s)
        self.refresh()

    def rotate(self):
        if not self.need_doc():
            return
        s = self.selected_only()
        for i in s:
            pg = self.doc[i]
            pg.set_rotation((pg.rotation + 90) % 360)
        self.refresh(s)

    def move(self, d):
        if not self.need_doc():
            return
        s = self.selected_only()
        if len(s) != 1:
            self.say("Taşımak için tek sayfa seçin.")
            return
        n = core.move_page(self.doc, s[0], d)
        self.refresh([n])

    def extract(self):
        if not self.need_doc():
            return
        s = self.selected_only()
        if not s:
            self.say("Ayrılacak sayfaları seçin.")
            return
        p, _ = QFileDialog.getSaveFileName(self, "Yeni PDF", "secili.pdf", "PDF (*.pdf)")
        if p:
            core.extract_pages(self.doc, s).save(p)
            self.say("Kaydedildi: " + p)

    def split(self):
        if not self.need_doc():
            return
        d = QFileDialog.getExistingDirectory(self, "Klasör seç")
        if not d:
            return
        base = os.path.splitext(os.path.basename(self.path or "belge"))[0]
        for i in self.sel_pages():
            core.extract_pages(self.doc, [i]).save(os.path.join(d, f"{base}-sayfa{i + 1}.pdf"))
        self.say("Sayfalar klasöre kaydedildi.")

    def edit_page(self, row=None):
        if not self.need_doc():
            return
        if not isinstance(row, int):
            s = self.selected_only()
            row = s[0] if s else 0
        PageEditor(self, row).exec()

    # ---- dönüştür
    def base(self):
        return os.path.splitext(os.path.basename(self.path or "belge"))[0]

    def convert_file(self, ext, label, fn):
        if not self.need_doc():
            return
        p, _ = QFileDialog.getSaveFileName(self, label, self.base() + ext, f"*{ext}")
        if not p:
            return
        pages, force, doc = self.sel_pages(), self.force_ocr.isChecked(), self.doc

        def job(progress):
            return fn(doc, pages, p, progress, force)

        def done(n):
            extra = f" ({n} metin parçası OCR ile düzeltildi; kontrol edin)" if n else ""
            self.say("Kaydedildi: " + p + extra)

        self.run_bg(job, done)

    def to_word(self):
        self.convert_file(".docx", "Word olarak kaydet",
                          lambda d, pg, p, pr, f: core.to_word(d, pg, p, self.ocr, pr, f))

    def to_excel(self):
        self.convert_file(".xlsx", "Excel olarak kaydet",
                          lambda d, pg, p, pr, f: core.to_excel(d, pg, p, self.ocr, pr, f))

    def to_ppt(self):
        self.convert_file(".pptx", "PowerPoint olarak kaydet",
                          lambda d, pg, p, pr, f: core.to_pptx(d, pg, p, pr))

    def to_img(self, fmt):
        if not self.need_doc():
            return
        d = QFileDialog.getExistingDirectory(self, "Resimlerin kaydedileceği klasör")
        if not d:
            return
        pages, doc, base = self.sel_pages(), self.doc, self.base()
        self.run_bg(lambda pr: core.to_images(doc, pages, d, fmt, base, pr),
                    lambda _r: self.say("Resimler kaydedildi: " + d))

    # ---- filigran
    def wm_add(self):
        if not self.need_doc():
            return
        dlg = WatermarkDialog(self)
        if dlg.exec() and dlg.text.text().strip():
            c = (dlg.color.redF(), dlg.color.greenF(), dlg.color.blueF())
            core.add_watermark(self.doc, dlg.text.text().strip(), dlg.size.value(), c, dlg.op.value() / 100,
                               dlg.angle.value())
            self.refresh()
            self.say("Filigran eklendi.")

    def wm_remove(self):
        if not self.need_doc():
            return
        t, ok = QInputDialog.getText(self, "Filigranı kaldır",
                                     "Filigran yazısı (boş bırakırsanız yalnızca işaretli filigranlar silinir):")
        if not ok:
            return
        n = core.remove_watermark(self.doc, t.strip() or None)
        self.refresh()
        self.say(f"{n} sayfadan filigran kaldırıldı." if n else "Kaldırılabilir filigran bulunamadı.")

    # ---- resimler
    def img_out(self):
        if not self.need_doc():
            return
        d = QFileDialog.getExistingDirectory(self, "Resimlerin kaydedileceği klasör")
        if d:
            n = core.extract_images(self.doc, self.sel_pages(), d)
            self.say(f"{n} resim kaydedildi." if n else "Çıkarılabilir resim bulunamadı.")

    def img_del(self):
        if not self.need_doc():
            return
        n = core.remove_images(self.doc, self.sel_pages())
        self.refresh()
        self.say(f"{n} sayfadaki resimler silindi." if n else "Silinecek resim bulunamadı.")


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
