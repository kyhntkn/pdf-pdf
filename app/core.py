"""PDF Studio çekirdeği: PDF işlemleri, OCR ile onarım, dönüştürmeler."""
import io
import os
import re
import shutil
import sys
import tempfile
from collections import Counter

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


def iter_spans(page, clip=None, raw=False):
    kind = "rawdict" if raw else "dict"
    d = page.get_text(kind, clip=clip) if clip else page.get_text(kind)
    for b in d["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if raw:
                    s["text"] = "".join(c["c"] for c in s["chars"])
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
def _base_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def find_tesseract():
    cands = [os.environ.get("TESSERACT_CMD"),
             os.path.join(_base_dir(), "tesseract", "tesseract.exe"),
             os.path.join(os.path.dirname(sys.executable), "tesseract", "tesseract.exe"),
             r"C:\Program Files\Tesseract-OCR\tesseract.exe",
             shutil.which("tesseract")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def clean_crop(img):
    """Tek satırlık yazı kırpığını Tesseract için hazırlar: gri, koyu yazı/açık zemin,
    kırpığın kenarına değen tablo çizgileri silinir (harf gövdelerine dokunulmaz)."""
    import cv2
    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    if np.median(g) < 128:  # koyu zeminde açık yazı (ör. kırmızı başlık)
        g = 255 - g
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    h, w = bw.shape
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(20, int(w * 0.8)), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(20, int(h * 0.92))))
    lines = cv2.bitwise_or(cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk), cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk))
    edge = np.zeros_like(bw)
    m = 10
    edge[:m, :] = 255
    edge[-m:, :] = 255
    edge[:, :m] = 255
    edge[:, -m:] = 255
    lines = cv2.dilate(cv2.bitwise_and(lines, edge), np.ones((3, 3), np.uint8))
    g = g.copy()
    g[lines > 0] = 255
    return np.pad(g, 12, constant_values=255)


class OCR:
    def __init__(self, lang="eng"):
        self.eng = None
        self.kind = None
        self.lang = lang

    def load(self):
        if self.eng:
            return
        err = None
        try:
            import pytesseract
            cmd = find_tesseract()
            if cmd:
                pytesseract.pytesseract.tesseract_cmd = cmd
                td = os.path.join(os.path.dirname(cmd), "tessdata")
                if os.path.isdir(td):
                    os.environ["TESSDATA_PREFIX"] = td
            pytesseract.get_tesseract_version()
            self.eng, self.kind = pytesseract, "tesseract"
            return
        except Exception as e:  # noqa
            err = e
        try:
            from rapidocr_onnxruntime import RapidOCR
            self.eng, self.kind = RapidOCR(), "rapid"
            return
        except Exception as e:  # noqa
            raise RuntimeError(f"OCR motoru yüklenemedi (Tesseract: {err}; RapidOCR: {e})")

    def read_page(self, img):
        """Tüm sayfayı okur. [(metin, x0, y0, x1, y1)] (piksel) döner. Yalnızca Tesseract ile."""
        self.load()
        if self.kind != "tesseract":
            return []
        import cv2
        from PIL import Image
        from pytesseract import Output
        g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        hk = cv2.getStructuringElement(cv2.MORPH_RECT, (120, 1))
        vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 120))
        lines = cv2.bitwise_or(cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk), cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk))
        thick = cv2.dilate(cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8)), np.ones((5, 5), np.uint8))
        lines = cv2.dilate(lines, np.ones((3, 3), np.uint8))
        lines[thick > 0] = 0  # dolu kırmızı başlık şeritlerine dokunma
        g = g.copy()
        g[lines > 0] = 255
        d = self.eng.image_to_data(Image.fromarray(g), lang=self.lang, output_type=Output.DICT,
                                   config="-c preserve_interword_spaces=1")
        out = []
        for i, t in enumerate(d["text"]):
            t = (t or "").strip()
            try:
                conf = float(d["conf"][i])
            except (TypeError, ValueError):
                conf = -1
            if t and conf >= 30:
                x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
                out.append((t, x, y, x + w, y + h))
        return out

    def read(self, img):
        """img: RGB numpy dizisi (tek satır yazı). (metin, güven) döner."""
        self.load()
        if self.kind == "tesseract":
            from PIL import Image
            g = clean_crop(img)
            txt = self.eng.image_to_string(Image.fromarray(g), lang=self.lang,
                                           config="--psm 7 -c preserve_interword_spaces=1")
            return txt.strip(), 1.0
        img = np.pad(img, ((10, 10), (10, 10), (0, 0)), constant_values=255)
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


def page_words(page, ocr, zoom=4):
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    words = ocr.read_page(np.ascontiguousarray(arr[:, :, :3]))
    return [(t, x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom) for t, x0, y0, x1, y1 in words]


def match_words(spans, words):
    """Sayfa OCR kelimelerini, taban çizgisi (origin) ve yatay aralığa göre span'lara dağıtır."""
    out = [[] for _ in spans]
    for t, wx0, wy0, wx1, wy1 in words:
        if not re.search(r"[A-Za-z0-9ÇĞİÖŞÜçğıöşü]", t):
            continue
        h = wy1 - wy0
        cx = (wx0 + wx1) / 2
        best = None
        for i, s in enumerate(spans):
            oy = s["origin"][1]
            bx0, _, bx1, _ = s["bbox"]
            if bx0 - 1.5 <= cx <= bx1 + 1.5 and wy0 + 0.45 * h <= oy <= wy1 + 0.2 * h:
                d = abs(oy - (wy1 - 0.2 * h))
                if best is None or d < best[0]:
                    best = (d, i)
        if best:
            out[best[1]].append((wx0, t))
    return [" ".join(t for _, t in sorted(lst)) for lst in out]


def span_size(s):
    sz = s["size"]
    if 3 <= sz <= 80:
        return sz
    r = fitz.Rect(s["bbox"])
    return max(4.0, r.height / 1.15)


def baseline_crop_rect(s):
    sz = span_size(s)
    ox, oy = s["origin"]
    r = fitz.Rect(s["bbox"])
    x1 = r.x1 if r.width > 2 else ox + sz * 0.5 * max(1, len(s["text"]))
    return fitz.Rect(ox - 1.0, oy - sz * 0.9, x1 + 1.0, oy + sz * 0.3)


def learn_maps(pairs):
    """(yazı tipi, bozuk metin, OCR metni) üçlülerinden karakter eşlemesi öğrenir (oylama)."""
    votes = {}
    for font, src, text in pairs:
        if src and len(src) == len(text):
            for a, b in zip(src, text):
                votes.setdefault(font, {}).setdefault(a, Counter())[b] += 1
    maps = {}
    for font, d in votes.items():
        m = {}
        for a, c in d.items():
            (b, n), tot = c.most_common(1)[0], sum(c.values())
            if (n >= 3 and n / tot >= 0.75) or (n == tot and n >= 2):
                m[a] = b
        maps[font] = m
    return maps


def decode_span(m, src, text):
    if src and len(src) == len(text):
        return "".join(m.get(a, b) for a, b in zip(src, text))
    if src and all(a in m for a in src):
        return "".join(m[a] for a in src)
    return text


RUN_RE = re.compile(r"(.)\1{5,}", re.S)


def split_span(s):
    """Span'ı, 6+ kez tekrarlayan karakter dizileri (çizgi/alt çizgi) ve yazı parçaları olarak böler."""
    chars, text = s.get("chars"), s["text"]
    if not chars:
        d = dict(s)
        d["run"] = False
        return [d]

    def mk(i, j, run):
        cs = chars[i:j]
        bb = [c["bbox"] for c in cs]
        d = dict(s)
        d["chars"], d["text"], d["run"] = cs, text[i:j], run
        d["bbox"] = (min(b[0] for b in bb), min(b[1] for b in bb), max(b[2] for b in bb), max(b[3] for b in bb))
        d["origin"] = tuple(cs[0]["origin"])
        return d

    out, pos = [], 0
    for m in RUN_RE.finditer(text):
        if m.start() > pos:
            out.append(mk(pos, m.start(), False))
        out.append(mk(m.start(), m.end(), True))
        pos = m.end()
    if pos < len(text):
        out.append(mk(pos, len(text), False))
    return out


def repair_doc(src, pages, ocr, progress=lambda m: None, force=False, log=None):
    """Seçili sayfaların kopyasını yapar; bozuk yazı tiplerindeki metni OCR ile düzeltir.
    (yeni belge, düzeltilen parça sayısı) döner."""
    log = log or (lambda m: None)
    out = fitz.open()
    for p in pages:
        out.insert_pdf(src, from_page=p, to_page=p)
    bad = find_bad_fonts(out, range(len(out)), force)
    log(f"Bozuk yazı tipleri: {sorted(bad)}")
    if not bad:
        return out, 0
    ocr.load()
    log(f"OCR motoru: {ocr.kind} / dil: {ocr.lang}")
    per_page, pairs = [], []
    for pi in range(len(out)):
        page = out[pi]
        origs = [s for s in iter_spans(page, raw=True) if s["font"] in bad and s["text"].strip()]
        items = []
        for so in origs:
            for pc in split_span(so):
                if pc["run"]:
                    items.append((pc, None))
                    continue
                if not pc["text"].strip():
                    continue
                progress(f"Sayfa {pages[pi] + 1}: OCR {len(items) + 1}")
                r = fitz.Rect(pc["bbox"])
                rr = fitz.Rect(r.x0 - 1.0, r.y0 - 0.5, r.x1 + 1.0, r.y1 + 0.5)
                text = clean_ocr(ocr.read(crop_image(page, rr))[0])
                items.append((pc, text))
                pairs.append((pc["font"], pc["text"].strip(), text))
        per_page.append((origs, items))
    maps = learn_maps(pairs)
    for f, m in maps.items():
        log(f"Eşleme {f}: {len(m)} karakter öğrenildi: " + "".join(f"{k!r}→{v}  " for k, v in list(m.items())[:60]))
    total = 0
    for pi, (origs, items) in enumerate(per_page):
        if not items:
            continue
        page = out[pi]
        apply_red(page, [s["bbox"] for s in origs])
        for pc, text in items:
            if pc["run"]:
                uw = get_font(pc["font"], pc["flags"]).text_length("_", pc["size"]) or 1
                width = pc["bbox"][2] - pc["bbox"][0]
                final = "_" * max(3, int(round(width / uw)))
            else:
                final = decode_span(maps.get(pc["font"], {}), pc["text"].strip(), text)
            log(f"s{pages[pi] + 1} {pc['font']} {pc['text'][:40]!r} | OCR={text!r} | SON={final!r}")
            if final:
                place_span(page, pc, final)
                total += 1
    return out, total


# ---------------------------------------------------------------- dönüştürmeler
def to_word(doc, pages, dst, ocr, progress=lambda m: None, force=False, log=None):
    from pdf2docx import Converter
    rep, n = repair_doc(doc, pages, ocr, progress, force, log)
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


def to_excel(doc, pages, dst, ocr, progress=lambda m: None, force=False, log=None):
    import openpyxl
    rep, n = repair_doc(doc, pages, ocr, progress, force, log)
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
