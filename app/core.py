"""PDF Studio çekirdeği: PDF işlemleri, OCR ile onarım, dönüştürmeler."""
import io
import os
import re
import tempfile

try:
    import pymupdf as fitz
except ImportError:  # eski PyMuPDF
    import fitz

import numpy as np

# ---------------------------------------------------------------- yazı tipleri
WIN_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
FAMILIES = {  # normal, kalın, italik, kalın+italik
    "times": ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    "arial": ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    "calibri": ("calibri.ttf", "calibrib.ttf", "calibrii.ttf", "calibriz.ttf"),
    "cour": ("cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"),
}
BASE14 = {
    "times": ("tiro", "tibo", "tiit", "tibi"),
    "arial": ("helv", "hebo", "heit", "hebi"),
    "calibri": ("helv", "hebo", "heit", "hebi"),
    "cour": ("cour", "cobo", "coit", "cobi"),
}
SYMBOL_FONT_RE = re.compile(r"wingding|webding|symbol|dingbat|marlett", re.I)


def family_of(name):
    n = (name or "").lower()
    if any(k in n for k in ("cour", "mono", "consol")):
        return "cour"
    if "calibri" in n:
        return "calibri"
    if "sans" not in n and any(k in n for k in ("times", "serif", "roman", "georgia", "garamond", "cambria", "minion")):
        return "times"
    return "arial"


def get_font(name="", flags=0, bold=None, italic=None):
    n = (name or "").lower()
    fam = family_of(name)
    b = bold if bold is not None else bool(flags & 16) or "bold" in n or "black" in n
    i = italic if italic is not None else bool(flags & 2) or "italic" in n or "oblique" in n
    idx = (1 if b else 0) + (2 if i else 0)
    path = os.path.join(WIN_FONTS, FAMILIES[fam][idx])
    if os.path.exists(path):
        return fitz.Font(fontfile=path)
    return fitz.Font(fontname=BASE14[fam][idx])


def symbol_font():
    p = os.path.join(WIN_FONTS, "seguisym.ttf")
    return fitz.Font(fontfile=p) if os.path.exists(p) else None


def put_text(page, x, y, text, font, size, color=(0, 0, 0), opacity=1.0, morph=None, max_width=None):
    if max_width:
        w = font.text_length(text, size)
        if w > max_width * 1.02 and w > 0:
            size = max(4.0, size * max_width / w)
    tw = fitz.TextWriter(page.rect)
    tw.append(fitz.Point(x, y), text, font=font, fontsize=size)
    tw.write_text(page, color=color, opacity=opacity, morph=morph)


# ---------------------------------------------------------------- redaksiyon
def apply_red(page, rects, cover=False, images=False, keep_text=False):
    for r in rects:
        page.add_redact_annot(fitz.Rect(r), fill=(1, 1, 1) if cover else False, cross_out=False)
    kw = {"images": fitz.PDF_REDACT_IMAGE_REMOVE if images else fitz.PDF_REDACT_IMAGE_NONE}
    if keep_text and hasattr(fitz, "PDF_REDACT_TEXT_NONE"):
        kw["text"] = fitz.PDF_REDACT_TEXT_NONE
    if hasattr(fitz, "PDF_REDACT_LINE_ART_NONE"):
        kw["graphics"] = fitz.PDF_REDACT_LINE_ART_NONE
    while True:
        try:
            page.apply_redactions(**kw)
            return
        except TypeError:
            if "graphics" in kw:
                kw.pop("graphics")
            elif "text" in kw:
                kw.pop("text")
            else:
                raise


# ---------------------------------------------------------------- bozuk yazı tipi tespiti
def garbled(text):
    s = re.sub(r"\s", "", text)
    if len(s) < 8:
        return False
    letters = len(re.findall(r"[A-Za-zÇĞİÖŞÜçğıöşü]", s))
    vowels = len(re.findall(r"[aeıioöuüAEIİOÖUÜ]", s))
    weird = len(re.findall(r"[^\w\s.,;:()'\"%\-–/&@+*°!?\[\]]", s))
    return letters / len(s) < 0.6 or vowels / max(letters, 1) < 0.18 or weird / len(s) > 0.15


def iter_spans(page, clip=None):
    d = page.get_text("dict", clip=clip) if clip else page.get_text("dict")
    for b in d["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                yield s


def find_bad_fonts(doc, page_numbers, force=False):
    acc = {}
    for p in page_numbers:
        for s in iter_spans(doc[p]):
            acc.setdefault(s["font"], []).append(s["text"])
    bad = set()
    for f, t in acc.items():
        if SYMBOL_FONT_RE.search(f):
            continue
        if force or garbled("".join(t)):
            bad.add(f)
    return bad


# ---------------------------------------------------------------- OCR
class OCR:
    def __init__(self):
        self.eng = None
        self.kind = None

    def load(self):
        if self.eng:
            return
        err = None
        try:
            from rapidocr_onnxruntime import RapidOCR
            self.eng, self.kind = RapidOCR(), "rapid"
            return
        except Exception as e:  # noqa
            err = e
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            self.eng, self.kind = pytesseract, "tess"
            return
        except Exception:  # noqa
            pass
        raise RuntimeError(f"OCR motoru yüklenemedi: {err}")

    def read(self, img):
        """img: RGB numpy dizisi (tek satır yazı). (metin, güven) döner."""
        self.load()
        img = np.pad(img, ((10, 10), (10, 10), (0, 0)), constant_values=255)
        if self.kind == "rapid":
            res = None
            try:
                res, _ = self.eng(img, use_det=False, use_cls=False, use_rec=True)
            except Exception:  # noqa
                res = None
            if res:
                it = res[0]
                if isinstance(it, (list, tuple)) and len(it) >= 2 and isinstance(it[0], str):
                    return it[0], float(it[1])
            res, _ = self.eng(img)
            if not res:
                return "", 0.0
            res = sorted(res, key=lambda r: r[0][0][0])
            return " ".join(r[1] for r in res), min(float(r[2]) for r in res)
        from PIL import Image
        return self.eng.image_to_string(Image.fromarray(img), config="--psm 7").strip(), 1.0


def crop_image(page, rect, zoom=4):
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect, alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    return np.ascontiguousarray(arr[:, :, :3])


BOX_PREFIX = re.compile(r"^(\[[IJ1lj|]?|[XOD0Q□口])(?=[A-Z][a-z]|O[a-z]|[A-Z]{2}|\s)")
YESNO_PREFIX = re.compile(r"^(\[[IJ1lj|]?|[OD0])(?=(?:yes|no)\b)", re.I)
BOX_ONLY = re.compile(r"^[\[\]|IJ1lj OXD0Q□口x]{1,3}$")


def clean_ocr(t):
    return re.sub(r"\s+", " ", t or "").strip()


def place_span(page, s, text):
    """Bozuk yazı tipli bir span'ı OCR metniyle, aynı konum/boyut/stil ile yeniden yazar."""
    size = s["size"]
    ox, oy = s["origin"]
    rect = fitz.Rect(s["bbox"])
    color = fitz.sRGB_to_pdf(s["color"])
    font = get_font(s["font"], s["flags"])
    sym = symbol_font()

    def box(ch_checked):
        if sym:
            return ("☒" if ch_checked else "☐"), sym
        return ("[x]" if ch_checked else "[ ]"), font

    square = rect.width < rect.height * 1.4
    if square and BOX_ONLY.match(text):
        g, f = box(text.strip().upper().startswith("X"))
        put_text(page, ox, oy, g, f, size, color)
        return
    prefix = None
    m = BOX_PREFIX.match(text) or YESNO_PREFIX.match(text)
    if m and len(text) - len(m.group(0)) >= 2:
        prefix = m.group(1)[:1].upper() == "X"
        text = text[len(m.group(0)):].lstrip()
    x = ox
    if prefix is not None:
        g, f = box(prefix)
        put_text(page, x, oy, g, f, size, color)
        x += size * 1.0
    if text:
        put_text(page, x, oy, text, font, size, color, max_width=max(rect.x1 - x, size))


def repair_doc(src, pages, ocr, progress=lambda m: None, force=False):
    """Seçili sayfaların kopyasını yapar; bozuk yazı tiplerindeki metni OCR ile düzeltir.
    (yeni belge, düzeltilen span sayısı) döner."""
    out = fitz.open()
    for p in pages:
        out.insert_pdf(src, from_page=p, to_page=p)
    bad = find_bad_fonts(out, range(len(out)), force)
    if not bad:
        return out, 0
    total = 0
    for pi in range(len(out)):
        page = out[pi]
        jobs = [s for s in iter_spans(page) if s["font"] in bad and s["text"].strip()]
        results = []
        for k, s in enumerate(jobs, 1):
            progress(f"Sayfa {pages[pi] + 1}: OCR {k}/{len(jobs)}")
            r = fitz.Rect(s["bbox"])
            rr = fitz.Rect(r.x0 - 1.5, r.y0 - 1, r.x1 + 1.5, r.y1 + 1)
            text, _score = ocr.read(crop_image(page, rr))
            results.append((s, clean_ocr(text)))
        if not results:
            continue
        apply_red(page, [s["bbox"] for s, _ in results])
        for s, text in results:
            if text:
                place_span(page, s, text)
                total += 1
    return out, total


# ---------------------------------------------------------------- dönüştürmeler
def to_word(doc, pages, dst, ocr, progress=lambda m: None, force=False):
    from pdf2docx import Converter
    rep, n = repair_doc(doc, pages, ocr, progress, force)
    fd, tmp = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        rep.save(tmp, garbage=3, deflate=True)
        rep.close()
        progress("Word dosyası oluşturuluyor…")
        cv = Converter(tmp)
        try:
            cv.convert(dst)
        finally:
            cv.close()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return n


def word_rows(page):
    rows = {}
    for x0, y0, x1, y1, w, b, l, _ in page.get_text("words"):
        rows.setdefault((b, l), []).append((x0, x1, w, y0))
    out = []
    for key, ws in sorted(rows.items(), key=lambda kv: min(w[3] for w in kv[1])):
        ws.sort()
        cells, cur, end = [], "", None
        for x0, x1, w, _y in ws:
            if end is not None and x0 - end > 8:
                cells.append(cur)
                cur = ""
            cur += (" " if cur else "") + w
            end = x1
        cells.append(cur)
        out.append(cells)
    return out


def to_excel(doc, pages, dst, ocr, progress=lambda m: None, force=False):
    import openpyxl
    rep, n = repair_doc(doc, pages, ocr, progress, force)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for i in range(len(rep)):
        page = rep[i]
        ws = wb.create_sheet(f"Sayfa {pages[i] + 1}")
        rows = []
        try:
            for t in page.find_tables().tables:
                rows += t.extract() + [[]]
        except Exception:  # noqa
            rows = []
        if not rows:
            rows = word_rows(page)
        for r in rows:
            ws.append(["" if c is None else c for c in r])
    wb.save(dst)
    rep.close()
    return n


def to_pptx(doc, pages, dst, progress=lambda m: None):
    from pptx import Presentation
    from pptx.util import Emu
    prs = Presentation()
    r0 = doc[pages[0]].rect
    prs.slide_width, prs.slide_height = Emu(int(r0.width * 12700)), Emu(int(r0.height * 12700))
    blank = prs.slide_layouts[6]
    for p in pages:
        progress(f"Slayt {p + 1}…")
        pix = doc[p].get_pixmap(dpi=170)
        s = prs.slides.add_slide(blank)
        s.shapes.add_picture(io.BytesIO(pix.tobytes("png")), 0, 0, prs.slide_width, prs.slide_height)
    prs.save(dst)


def to_images(doc, pages, outdir, fmt, base, progress=lambda m: None):
    for p in pages:
        progress(f"Sayfa {p + 1}…")
        pix = doc[p].get_pixmap(dpi=200, alpha=False)
        pix.save(os.path.join(outdir, f"{base}-sayfa{p + 1}.{fmt}"))


# ---------------------------------------------------------------- sayfa işlemleri
def extract_pages(doc, pages):
    new = fitz.open()
    for p in pages:
        new.insert_pdf(doc, from_page=p, to_page=p)
    return new


def move_page(doc, pno, delta):
    to = pno + delta
    if to < 0 or to >= len(doc):
        return pno
    target = to if delta < 0 else (to + 1 if to + 1 < len(doc) else -1)
    doc.move_page(pno, target)
    return to


# ---------------------------------------------------------------- filigran
START_RE = re.compile(r"/PDFStudioWM\s+BMC|/Artifact\s*<<[^>]*?/Subtype\s*/Watermark[^>]*?>>\s*BDC")
TOKEN_RE = re.compile(r"\b(BDC|BMC|EMC)\b")


def strip_blocks(s):
    out, last, pos = [], 0, 0
    while True:
        m = START_RE.search(s, pos)
        if not m:
            break
        depth, end = 1, -1
        for t in TOKEN_RE.finditer(s, m.end()):
            depth += -1 if t.group(1) == "EMC" else 1
            if depth == 0:
                end = t.end()
                break
        if end < 0:
            break
        out.append(s[last:m.start()])
        last = pos = end
    out.append(s[last:])
    return "".join(out)


def add_watermark(doc, text, size, color, opacity, angle):
    font = get_font("Arial")
    for page in doc:
        before = set(page.get_contents())
        r = page.rect
        sz = float(size)
        w = font.text_length(text, sz)
        if w > r.width * 0.9:
            sz *= r.width * 0.9 / w
            w = font.text_length(text, sz)
        cx, cy = r.width / 2, r.height / 2
        tw = fitz.TextWriter(page.rect)
        tw.append(fitz.Point(cx - w / 2, cy + sz * 0.3), text, font=font, fontsize=sz)
        tw.write_text(page, color=color, opacity=opacity, morph=(fitz.Point(cx, cy), fitz.Matrix(-angle)))
        new = [x for x in page.get_contents() if x not in before]
        if new:
            first = doc.xref_stream(new[0])
            doc.update_stream(new[0], b"/PDFStudioWM BMC\n" + first)
            last = doc.xref_stream(new[-1])
            doc.update_stream(new[-1], last + b"\nEMC")


def remove_watermark(doc, text=None):
    changed = 0
    for page in doc:
        hit = False
        data = page.read_contents()
        s = data.decode("latin-1")
        t = strip_blocks(s)
        if t != s:
            xr = page.get_contents()
            doc.update_stream(xr[0], t.encode("latin-1"))
            for x in xr[1:]:
                doc.update_stream(x, b"")
            hit = True
        if text:
            rects = page.search_for(text)
            if rects:
                apply_red(page, rects)
                hit = True
        changed += hit
    return changed


# ---------------------------------------------------------------- resimler
def extract_images(doc, pages, outdir):
    n = 0
    for p in pages:
        for k, img in enumerate(doc[p].get_images(full=True), 1):
            info = doc.extract_image(img[0])
            if not info or info.get("width", 0) < 24 or info.get("height", 0) < 24:
                continue
            with open(os.path.join(outdir, f"sayfa{p + 1}-resim{k}.{info['ext']}"), "wb") as f:
                f.write(info["image"])
            n += 1
    return n


def remove_images(doc, pages):
    n = 0
    for p in pages:
        page = doc[p]
        rects = []
        for img in page.get_images(full=True):
            rects += list(page.get_image_rects(img[0]))
        if rects:
            apply_red(page, rects, images=True, keep_text=True)
            n += 1
    return n


# ---------------------------------------------------------------- metin düzenleme
def read_rect_text(page, rect, ocr):
    spans = list(iter_spans(page, clip=rect))
    if not spans:
        return clean_ocr(ocr.read(crop_image(page, rect))[0])
    bad = find_bad_fonts(page.parent, [page.number])
    if any(s["font"] in bad for s in spans):
        return clean_ocr(ocr.read(crop_image(page, rect))[0])
    return clean_ocr(page.get_text("text", clip=rect))


def replace_rect_text(page, rect, new_text, cover=True, size=14, color=(0, 0, 0)):
    spans = [s for s in iter_spans(page, clip=rect) if s["text"].strip()]
    apply_red(page, [rect], cover=cover)
    if not new_text:
        return
    if spans:
        s = spans[0]
        font = get_font(s["font"], s["flags"])
        put_text(page, s["origin"][0], s["origin"][1], new_text, font, s["size"], fitz.sRGB_to_pdf(s["color"]),
                 max_width=max(rect.x1 - s["origin"][0], s["size"]))
    else:
        put_text(page, rect.x0, rect.y1 - size * 0.25, new_text, get_font("Arial"), size, color, max_width=rect.width)


def add_text(page, rect, text, size, color):
    put_text(page, rect.x0, rect.y0 + size, text, get_font("Arial"), size, color, max_width=rect.width)


def add_image(page, rect, path):
    page.insert_image(rect, filename=path, keep_proportion=True, overlay=True)
