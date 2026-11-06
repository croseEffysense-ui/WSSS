"""Extraction « par colonnes » : s'appuie sur la position (x, y) de chaque mot du PDF et sur la ligne
d'en-tête du tableau (« Quantité », « Prix Net »…) pour savoir quel nombre va dans quel champ.

Fonctionne quel que soit le nombre de colonnes de la facture : les colonnes inutiles (Remise, TVA,
Taille…) sont simplement ignorées, et les en-têtes sur plusieurs lignes (« Prix / Catalogue ») sont recollés.
Deux usages :
- automatique : les en-têtes sont reconnus grâce à des libellés courants (HEADER_SYNONYMS) ;
- modèle fournisseur : {champ: libellé d'en-tête} enregistré dans la config, pour les libellés exotiques.
Chaque décision (ligne d'en-tête, article, suite de description, pied de tableau…) est tracée pour le mode debug.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field, replace
from statistics import median
from typing import Optional

from .formats import TABLE_FIELDS, normalize_label
from .models import LineItem
from .pdf_parser import PdfExtractionError

NUMERIC_FIELDS = ("qty", "unit_price_gross", "unit_price_net", "total", "public_price")

# Libellés d'en-tête reconnus en mode automatique (regex sur le libellé normalisé : minuscules, sans accents
# ni ponctuation). Pour chaque champ, du plus spécifique au plus général : le premier motif qui trouve une
# colonne gagne (ex. « Qté liv » est préféré à « Qté cde », « Montant Net » à « Montant Total »).
HEADER_SYNONYMS: dict[str, list[str]] = {
    "sku": [
        r"^(numero d |no |n )?article$",
        r"^(code|ref|reference|sku|style code|code article|item|item code|article no|art no|no article)\b",
        r"\b(ref|reference|sku)\b",
    ],
    "qty": [
        r"^(qte|qty|quantite|quantity|qt)\b.*\b(liv|livree|facturee|shipped|delivered|invoiced)\b",
        r"^(qte|qty|quantite|quantity|qt)( totale| total)?$",
        r"^(qte|qty|quantite|quantity|qt|pcs|units?)\b",
    ],
    "unit_price_net": [r"^(prix|pu|p u|unit price|price)\b.*\bnet\b", r"^net price\b"],
    "public_price": [r"\b(prix public|ppc|pvc|ppi|rrp|msrp|retail|prix de vente)\b"],
    "total": [
        r"^(montant|total|mont)\b.*\b(net|ht)\b",
        r"^mont\b.*\bcharg",
        r"^(montant|total|amount|line total|total ligne|net amount)$",
    ],
    "unit_price_gross": [
        r"^(prix|pu|p u|unit price|price|tarif)\b.*\b(catalogue|brut|ht|tarif|pc|unitaire|ha)\b",
        r"^(prix|price|pu|tarif)$",
        r"^prix\b",
    ],
    "description": [r"^(description|designation|libelle|desc)\b", r"^(produit|product|nom|name)\b"],
    "sizes": [r"^(taille|size|pointure)s?\b.*\b(quantite|qte|qty|quantity)", r"^(tailles?|sizes?|pointures?)$"],
}

NUMBER_RE = re.compile(r"^[€$£]?-?\d+(?:[ .,]\d{3})*(?:[.,]\d+)?-?%?€?$")
BARCODE_RE = re.compile(r"^(\d{8}|\d{12,14})$")  # EAN-8, UPC-12, EAN-13, GTIN-14
SKU_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{2,}$")
INLINE_SKU_RE = re.compile(r"^(sku|ref|r[ée]f|r[ée]f[ée]rence|art|article|item|code)[.:]?$", re.I)
FOOTER_RE = re.compile(
    r"^(sous[- ]?total|total|montant total|montant ht|net [aà] payer|subtotal|tva|vat|report|a reporter)\b", re.I
)
# « 3913 / 1 », « 42/2 », « M / 3 » : une taille, une barre, une quantité entière
SIZE_PAIR_RE = re.compile(r"(?<!\S)([A-Za-z0-9][A-Za-z0-9.,½⅓⅔+]*)\s*/\s*(\d{1,4})(?=\s|$)")
# grille sous l'article (ex. Encore) : « Taille:  5  5.5  6 » puis « Quantité:  2  2  1 », alignées colonne par colonne
GRID_SIZE_LABEL_RE = re.compile(r"^(tailles?|sizes?|pointures?)\s*:$", re.I)
GRID_QTY_LABEL_RE = re.compile(r"^(quantit[ée]s?|qt[ée]s?|qty|quantity)\s*:$", re.I)
FRACTION_SIZE_RE = re.compile(r"^([1-5]\d)(13|23)$")  # pointures Adidas : 3913 = 39 1/3, 4223 = 42 2/3
NOISE_RE = re.compile(
    r"(intrastat|intra\.\d|origine\s*:|country of origin|hs code|commande client|num[ée]ro de livraison|votre r[ée]f|"
    r"no\.?\s*cmmde|\bno\.?\s+(commande|d'envoi)\b|date de livraison|^page\b|^\d+\s*/\s*\d+$)",
    re.I,
)

ROLE_OUTSIDE = "hors tableau"
ROLE_HEADER = "en-tête"
ROLE_ITEM = "article"
ROLE_CONT = "suite d'article"
ROLE_FOOTER = "pied de tableau"
ROLE_IGNORED = "ignorée"


# ----------------------------- Structures -----------------------------
@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Line:
    page: int
    words: list[Word]

    @property
    def top(self) -> float:
        return min(w.top for w in self.words)

    @property
    def bottom(self) -> float:
        return max(w.bottom for w in self.words)

    @property
    def height(self) -> float:
        return median(w.bottom - w.top for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Column:
    label: str
    x0: float
    x1: float
    field: Optional[str] = None
    lo: float = float("-inf")  # zone horizontale du texte de la colonne (mi-chemin avec les voisines)
    hi: float = float("inf")

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2

    def holds(self, w: Word) -> bool:
        return self.lo <= w.xc < self.hi

    def distance(self, w: Word) -> float:
        """Écart d'un nombre à l'en-tête : aligné à droite OU centré (les deux usages existent)."""
        return min(abs(w.x1 - self.x1), abs(w.xc - self.center))


@dataclass
class LineTrace:
    page: int
    y: float
    text: str
    role: str
    detail: str = ""


@dataclass
class TableTrace:
    """Tout ce qu'il faut pour comprendre une extraction (affiché en mode debug)."""

    columns: list[Column] = field(default_factory=list)
    lines: list[LineTrace] = field(default_factory=list)
    cells: list[dict[str, str]] = field(default_factory=list)  # par article : colonne -> texte lu sur la ligne
    problem: str = ""


# ----------------------------- Lecture du PDF -----------------------------
def extract_pages(pdf_bytes: bytes) -> list[list[Line]]:
    """PDF -> pages -> lignes de mots positionnés (pdfplumber)."""
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dépendance déclarée dans requirements.txt
        raise PdfExtractionError("Module « pdfplumber » manquant : pip install pdfplumber") from exc
    pages: list[list[Line]] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for n, page in enumerate(pdf.pages, start=1):
                raw = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
                words = [Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"]) for w in raw if w["text"].strip()]
                pages.append(group_lines(words, n))
    except Exception as exc:  # noqa: BLE001 - pdfminer lève des exceptions très variées
        raise PdfExtractionError(f"Lecture des positions impossible : {type(exc).__name__}: {exc}") from exc
    return pages


def pages_text(pages: list[list[Line]]) -> str:
    """Texte brut reconstitué à partir des lignes positionnées (repli quand pypdf ne trouve rien)."""
    return "\n".join(line.text for lines in pages for line in lines)


def group_lines(words: list[Word], page: int) -> list[Line]:
    """Regroupe les mots qui se chevauchent verticalement (tolère les décalages de police d'une même ligne)."""
    lines: list[Line] = []
    for w in sorted(words, key=lambda w: (w.top, w.x0)):
        h = max(w.bottom - w.top, 1.0)
        if lines:
            cur = lines[-1]
            overlap = min(cur.bottom, w.bottom) - max(cur.top, w.top)
            if overlap >= 0.5 * min(h, cur.height):
                cur.words.append(w)
                continue
        lines.append(Line(page, [w]))
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
    return lines


def cells(line: Line) -> list[Column]:
    """Fusionne les mots séparés d'à peine plus d'un espace : « Prix Net » = une cellule, « Net   Montant » = deux."""
    chars = sum(len(w.text) for w in line.words)
    char_w = sum(w.x1 - w.x0 for w in line.words) / max(chars, 1)
    out: list[Column] = []
    for w in line.words:
        if out and w.x0 - out[-1].x1 <= 1.3 * char_w:
            last = out[-1]
            last.label, last.x1 = f"{last.label} {w.text}", max(last.x1, w.x1)
        else:
            out.append(Column(w.text, w.x0, w.x1))
    return out


# ----------------------------- En-tête du tableau -----------------------------
def assign_fields(columns: list[Column], mapping: Optional[dict[str, str]] = None) -> None:
    """Associe un champ à chaque colonne, soit d'après un modèle {champ: libellé}, soit d'après HEADER_SYNONYMS."""
    for c in columns:
        c.field = None
    if mapping:
        # « Produit + Couleur » : la description peut réunir plusieurs colonnes
        targets = [(fld, t) for fld, label in mapping.items() for t in mapping_targets(fld, label)]
        found: set[tuple[str, str]] = set()
        # libellés identiques d'abord, puis approchants : « Prix » ne doit pas capter la colonne « Prix Net »
        for exact in (True, False):
            for fld, target in targets:
                if (fld, target) in found:
                    continue
                for c in columns:
                    lab = normalize_label(c.label)
                    if c.field is None and (lab == target if exact else (target in lab or lab in target)):
                        c.field = fld
                        found.add((fld, target))
                        break
        return
    for fld, patterns in HEADER_SYNONYMS.items():
        for pattern in patterns:
            hit = next((c for c in columns if c.field is None and re.search(pattern, normalize_label(c.label))), None)
            if hit is not None:
                hit.field = fld
                break


def mapping_targets(fld: str, label: str) -> list[str]:
    """Libellés normalisés visés par un champ du modèle (plusieurs pour « Description = Produit + Couleur »)."""
    parts = label.split("+") if fld == "description" else [label]
    return [t for t in (normalize_label(p) for p in parts) if t]


def _set_zones(columns: list[Column]) -> None:
    columns.sort(key=lambda c: c.x0)
    for i, c in enumerate(columns):
        c.lo = (columns[i - 1].x1 + c.x0) / 2 if i else float("-inf")
        c.hi = (c.x1 + columns[i + 1].x0) / 2 if i + 1 < len(columns) else float("inf")
        if c.field == "description":  # la désignation (alignée à gauche) déborde à droite jusqu'à la colonne suivante
            c.hi = columns[i + 1].x0 if i + 1 < len(columns) else float("inf")


def _merge_header(lines: list[Line]) -> list[Column]:
    """Recolle un en-tête sur plusieurs lignes : « Prix » au-dessus de « Catalogue » -> « Prix Catalogue »."""
    columns: list[Column] = []
    for line in lines:
        for cell in cells(line):
            hit = next((c for c in columns if cell.x0 < c.x1 and c.x0 < cell.x1), None)
            if hit:
                hit.label, hit.x0, hit.x1 = f"{hit.label} {cell.label}", min(hit.x0, cell.x0), max(hit.x1, cell.x1)
            else:
                columns.append(cell)
    return sorted(columns, key=lambda c: c.x0)


def header_columns(lines: list[Line], mapping: Optional[dict[str, str]] = None) -> list[Column]:
    """Colonnes (champs + zones) d'un en-tête choisi à la main (formulaire de création de modèle)."""
    columns = _merge_header(lines)
    assign_fields(columns, mapping)
    _set_zones(columns)
    return columns


PRICE_FIELDS = {"unit_price_gross", "unit_price_net", "total", "public_price"}


def _header_score(columns: list[Column]) -> int:
    """Nombre de champs reconnus ; 0 si l'essentiel manque.

    En-tête valable : Qté + référence ou désignation, ou bien (quantités dans une grille « Taille: / Quantité: »
    sous chaque article, ex. Encore) référence + au moins un prix. Un en-tête avec Qté passe toujours devant.
    """
    fields = {c.field for c in columns if c.field}
    if "qty" in fields and fields & {"sku", "description"}:
        return 10 + len(fields)
    if "sku" in fields and fields & PRICE_FIELDS and len(fields) >= 3:
        return len(fields)
    return 0


def find_header(lines: list[Line], mapping: Optional[dict[str, str]] = None) -> Optional[tuple[int, int, list[Column]]]:
    """Cherche le bloc de 1 à 3 lignes (sans chiffres, rapprochées) qui ressemble le plus à un en-tête de tableau.

    Retourne (index de début, index de fin exclu, colonnes avec champs et zones) ou None.
    """
    best: Optional[tuple[tuple[int, int, int], int, int, list[Column]]] = None
    targets = {k: mapping_targets(k, v) for k, v in (mapping or {}).items()}
    for i in range(len(lines)):
        for size in (1, 2, 3):
            block = lines[i : i + size]
            if len(block) < size or any(re.search(r"\d", ln.text) for ln in block):
                break
            if size > 1 and block[-1].top - block[-2].bottom > 1.2 * block[-2].height:
                break
            columns = _merge_header(block)
            assign_fields(columns, mapping)
            score = _header_score(columns)
            # à égalité, le bloc dont les libellés collent exactement au modèle (en-tête complet), puis le plus court
            exact = sum(1 for c in columns if c.field and normalize_label(c.label) in targets.get(c.field, []))
            if score and (best is None or (score, exact, -size) > best[0]):
                best = ((score, exact, -size), i, i + size, columns)
    if best is None:
        return None
    _, start, end, columns = best
    _set_zones(columns)
    return start, end, columns


# ----------------------------- Lignes de données -----------------------------
def parse_amount(token: str) -> Optional[float]:
    """« 1.050,30 » -> 1050.3 ; « €2223.76 » -> 2223.76 ; « 28,01- » -> -28.01 ; « 20,00% » -> 20.0."""
    t = token.strip().replace(" ", "").replace(" ", "")
    if not NUMBER_RE.match(t):
        return None
    t = t.strip("€$£%")
    neg = t.endswith("-") or t.startswith("-")
    t = t.strip("-")
    if "," in t and "." in t:  # le dernier séparateur est la décimale
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        t = t.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in t:
        t = t.replace(",", ".") if t.count(",") == 1 else t.replace(",", "")
    elif t.count(".") > 1:  # 1.234.567
        t = t.replace(".", "")
    try:
        value = float(t)
    except ValueError:
        return None
    return -value if neg else value


def by_field(columns: list[Column]) -> dict[str, Column]:
    return {c.field: c for c in columns if c.field}


def _sku_word(line: Line, fields: dict[str, Column]) -> Optional[Word]:
    """Référence : sous la colonne SKU (1er mot avec un chiffre, sinon code en majuscules seul),
    ou bien juste après un libellé « SKU: », « Réf. »… dans la désignation (ex. VPM)."""
    col = fields.get("sku")
    if col is not None:
        zone = [w for w in line.words if col.holds(w) and SKU_TOKEN_RE.match(w.text) and not w.text.endswith(":")]
        with_digit = [w for w in zone if any(ch.isdigit() for ch in w.text) and not BARCODE_RE.match(w.text)]
        if with_digit:
            return with_digit[0]
        if len(zone) == 1 and zone[0].text.isupper() and len(zone[0].text) >= 4:
            return zone[0]
    for label, token in zip(line.words, line.words[1:]):
        if INLINE_SKU_RE.match(label.text) and SKU_TOKEN_RE.match(token.text) and any(ch.isdigit() for ch in token.text):
            return token
    return None


def decode_size(code: str) -> str:
    """Code de taille de la facture -> taille lisible : « 3913 » -> « 39 1/3 », « 4223 » -> « 42 2/3 », sinon inchangé."""
    m = FRACTION_SIZE_RE.match(code)
    return f"{m.group(1)} {'1/3' if m.group(2) == '13' else '2/3'}" if m else code


def _size_pairs(line: Line, columns: list[Column]) -> list[tuple[str, int, list[Word]]]:
    """Paires (code taille, quantité, mots) de la colonne « Tailles / Quantités ».

    La répartition déborde souvent largement de son en-tête (elle commence sous « Quantité Totale ») :
    on la cherche entre les centres des colonnes voisines.
    """
    col = by_field(columns).get("sizes")
    if col is None:
        return []
    i = columns.index(col)
    lo = columns[i - 1].center if i else float("-inf")
    hi = columns[i + 1].center if i + 1 < len(columns) else float("inf")
    text, spans = "", []
    for w in line.words:
        if lo <= w.xc < hi:
            spans.append((len(text), len(text) + len(w.text), w))
            text += w.text + " "
    return [
        (m.group(1), int(m.group(2)), [w for s, e, w in spans if s < m.end() and e > m.start()])
        for m in SIZE_PAIR_RE.finditer(text)
    ]


def grid_row(line: Line) -> Optional[tuple[str, list[Word]]]:
    """Ligne d'une grille de tailles : ("sizes" | "qty", mots des valeurs après le libellé) ou None."""
    for i, w in enumerate(line.words):
        values = line.words[i + 1 :]
        if values and GRID_SIZE_LABEL_RE.match(w.text):
            return "sizes", values
        if values and GRID_QTY_LABEL_RE.match(w.text) and all(parse_amount(v.text) is not None for v in values):
            return "qty", values
    return None


def _grid_pairs(lines: list[Line]) -> list[tuple[str, int, list[Word]]]:
    """Grille « Taille: / Quantité: » -> paires (taille, quantité) : chaque quantité va à la taille au-dessus d'elle."""
    sizes: list[Word] = []
    out: list[tuple[str, int, list[Word]]] = []
    for ln in lines:
        row = grid_row(ln)
        if row and row[0] == "sizes":
            sizes = row[1]
        elif row and row[0] == "qty" and sizes:
            for q in row[1]:
                value = parse_amount(q.text)
                if value and value > 0:
                    size = min(sizes, key=lambda s: abs(s.xc - q.xc))
                    out.append((size.text, int(value), [size, q]))
            sizes = []
    return out


def _size_words(line: Line, columns: list[Column]) -> set[int]:
    return {id(w) for _, _, words in _size_pairs(line, columns) for w in words}


def _numeric(line: Line, fld: str, columns: list[Column], skip: Optional[set[int]] = None) -> Optional[Word]:
    """Nombre de la ligne rangé sous la colonne `fld` : chaque nombre va à l'en-tête le plus proche,
    et si plusieurs nombres tombent sous la même colonne, le plus proche gagne. Les mots de la répartition
    par taille (« 40 / 1 ») ne sont jamais pris pour une quantité ou un prix."""
    skip = _size_words(line, columns) if skip is None else skip
    best: Optional[tuple[float, Word]] = None
    for w in line.words:
        if id(w) in skip or parse_amount(w.text) is None or BARCODE_RE.match(w.text):
            continue
        col = min(columns, key=lambda c: c.distance(w))
        if col.field == fld and (best is None or col.distance(w) < best[0]):
            best = (col.distance(w), w)
    return best[1] if best else None


def _is_footer(line: Line) -> bool:
    return any(FOOTER_RE.match(c.label) for c in cells(line))


def parse_pages(pages: list[list[Line]], mapping: Optional[dict[str, str]] = None) -> tuple[list[LineItem], TableTrace]:
    """Pages de lignes positionnées -> articles + trace détaillée.

    Une ligne « article » = une référence + une quantité. Les autres lignes du tableau sont rattachées ainsi :
    - collée sous un article -> suite de sa désignation ;
    - collée au-dessus d'un article, après un blanc -> début de sa désignation (ex. Santa Cruz, VPM) ;
    - en haut de page, avant le premier article -> suite du dernier article de la page précédente (ex. V7).
    """
    trace = TableTrace()
    columns: list[Column] = []
    rows: list[tuple[Line, list[Line]]] = []  # (ligne article, lignes de suite)
    inside = False
    traces: dict[int, LineTrace] = {}

    def mark(ln: Line, role: str, detail: str) -> None:
        traces[id(ln)].role, traces[id(ln)].detail = role, detail

    def flush(pending: list[Line], page_top: bool) -> None:
        # une quantité sans référence = ligne de regroupement / sous-total (ex. VPM « 1 PM - Red LTR » + « 6 336.00 ») :
        # ce n'est pas la suite de l'article de la page précédente
        group_row = "qty" in by_field(columns) and any(_numeric(ln, "qty", columns) for ln in pending)
        for ln in pending:
            if group_row:
                mark(ln, ROLE_IGNORED, "ligne de regroupement (quantité sans référence)")
            elif page_top and rows:
                rows[-1][1].append(ln)
                mark(ln, ROLE_CONT, f"haut de page : suite de {rows[-1][0].text.split()[0]}")
            else:
                mark(ln, ROLE_IGNORED, "isolée par des blancs, sans article collé")
        pending.clear()

    for lines in pages:
        header = find_header(lines, mapping)
        h_start, h_end = (header[0], header[1]) if header else (-1, -1)
        if header:
            columns = header[2]
            trace.columns = columns
        fields = by_field(columns)
        pending: list[Line] = []
        prev: Optional[Line] = None
        page_has_item = False
        for idx, line in enumerate(lines):
            lt = traces[id(line)] = LineTrace(line.page, round(line.top, 1), line.text, ROLE_OUTSIDE)
            trace.lines.append(lt)
            if h_start <= idx < h_end:
                lt.role = ROLE_HEADER
                lt.detail = " | ".join(f"{c.label} → {TABLE_FIELDS[c.field]}" if c.field else c.label for c in columns)
                inside, prev = True, None
                continue
            if not inside or not fields or idx < h_start:
                continue
            gap = prev is not None and line.top - prev.bottom > 0.9 * prev.height
            prev = line
            if gap and pending:
                flush(pending, not page_has_item)
            grid = grid_row(line)
            if grid and rows and page_has_item:  # « Taille: 5 5.5… » / « Total ht ligne: 945€ Quantité: 2 2… »
                rows[-1][1].append(line)
                lt.role, lt.detail = ROLE_CONT, f"grille {'des tailles' if grid[0] == 'sizes' else 'des quantités'} de {rows[-1][0].text.split()[0]}"
                continue
            sku = _sku_word(line, fields)
            if "qty" in fields:
                qty = _numeric(line, "qty", columns)
            else:  # quantités dans une grille sous l'article : la ligne article porte la référence et un prix
                qty = next((w for f in PRICE_FIELDS & set(fields) if (w := _numeric(line, f, columns))), None)
            if not (sku and qty) and _is_footer(line):
                lt.role, lt.detail = ROLE_FOOTER, "fin du tableau (total / sous-total)"
                flush(pending, not page_has_item)
                inside = False
            elif NOISE_RE.search(line.text) and not _size_pairs(line, columns):  # « 4423 / 1 » n'est pas « page 1/4 »
                lt.role, lt.detail = ROLE_IGNORED, "ligne d'information (n° commande, douane, origine, page…)"
            elif sku and qty:
                lt.role = ROLE_ITEM
                lt.detail = f"SKU {sku.text} · " + (f"Qté {qty.text}" if "qty" in fields else f"prix {qty.text}, quantités dans la grille")
                for ln in pending:
                    mark(ln, ROLE_CONT, f"début de désignation de {sku.text}")
                rows.append((line, list(pending)))
                pending.clear()
                page_has_item = True
            elif rows and page_has_item and not gap and not pending:
                rows[-1][1].append(line)
                lt.role, lt.detail = ROLE_CONT, f"suite de {rows[-1][0].text.split()[0]}"
            else:
                pending.append(line)
                lt.role, lt.detail = ROLE_IGNORED, "en attente : rattachée à l'article suivant s'il est collé"
        flush(pending, not page_has_item)

    if not columns:
        trace.problem = (
            "Aucune ligne d'en-tête de tableau reconnue : il faut au moins une colonne Quantité/Qté et une colonne "
            "Référence/Code/Article ou Désignation (ou un modèle fournisseur dont les libellés correspondent)."
        )
        return [], trace
    items = [it for anchor, extra in rows for it in _build_items(anchor, extra, columns, trace)]
    if not items:
        trace.problem = "En-tête trouvé, mais aucune ligne ne contient à la fois une référence et une quantité sous ces colonnes."
    return items, trace


def _build_items(anchor: Line, extra: list[Line], columns: list[Column], trace: TableTrace) -> list[LineItem]:
    """Un article de la facture -> une ligne, ou une ligne par taille s'il a une répartition « 40 / 1  42 / 2 »
    ou une grille « Taille: / Quantité: »."""
    fields = by_field(columns)
    sku_col, size_col = fields.get("sku"), fields.get("sizes")
    desc_cols = [c for c in columns if c.field == "description"]
    ordered = sorted([anchor, *extra], key=lambda l: (l.page, l.top))
    grid_lines = [ln for ln in ordered if grid_row(ln)]
    sku_word = _sku_word(anchor, fields)
    item = LineItem(sku=sku_word.text if sku_word else "")
    used: set[int] = {id(sku_word)}
    if sku_word is not None and (sku_col is None or not sku_col.holds(sku_word)):  # « SKU: F1100 » dans la désignation
        i = anchor.words.index(sku_word)
        used.add(id(anchor.words[i - 1]))
    pairs = [p for ln in ordered for p in _size_pairs(ln, columns)] or _grid_pairs(grid_lines)
    used |= {id(w) for _, _, words in pairs for w in words}
    used |= {id(w) for ln in grid_lines for w in grid_row(ln)[1]}

    for fld in NUMERIC_FIELDS:
        if fld not in fields:
            continue
        word = _numeric(anchor, fld, columns, used) or next(
            (w for ln in extra if (w := _numeric(ln, fld, columns, used))), None
        )
        if word is None:
            continue
        used.add(id(word))
        value = parse_amount(word.text)
        if fld == "qty":
            if value != int(value):
                item.warnings.append(f"Quantité non entière : {word.text}")
            item.qty = int(value)
        else:
            setattr(item, fld, value)

    if size_col is not None and not pairs:  # colonne « Taille » simple : une taille par ligne
        size = " ".join(w.text for w in anchor.words if size_col.holds(w) and id(w) not in used)
        item.size = size
        used |= {id(w) for w in anchor.words if size_col.holds(w)}

    desc: list[str] = []
    for ln in ordered:
        if ln in grid_lines:  # « Produits: 21  Taille: … » n'est pas de la désignation
            continue
        # sur la ligne article, la désignation déborde souvent sous l'en-tête Référence ; pas sur les lignes de suite
        zones = [*desc_cols, *([sku_col] if sku_col is not None and (ln is anchor or not desc_cols) else [])]
        for w in ln.words:
            if id(w) in used:
                continue
            if BARCODE_RE.match(w.text):
                item.barcode = item.barcode or w.text
            elif any(z.holds(w) for z in zones):
                desc.append(w.text)
    item.description = " ".join(desc)

    tol = lambda total: max(0.05, 0.005 * abs(total))  # noqa: E731
    if item.unit_price_net is None and item.unit_price_gross is not None:  # un seul prix d'achat sur la facture
        item.unit_price_net = item.unit_price_gross
        if item.qty and item.total is not None and abs(item.qty * item.unit_price_gross - item.total) > tol(item.total):
            item.unit_price_net = round(item.total / item.qty, 2)  # remise appliquée : coût réel = Total / Qté
    cells_row = {c.label: " ".join(w.text for w in anchor.words if c.holds(w)) for c in columns}
    if pairs:
        cells_row["→ Tailles lues"] = ", ".join(f"{decode_size(code)} ×{q}" for code, q, _ in pairs)
    trace.cells.append(cells_row)
    if item.qty <= 0 and not pairs:
        item.warnings.append("Quantité absente ou nulle")
    if item.qty and item.unit_price_net is not None and item.total is not None:
        if abs(item.qty * item.unit_price_net - item.total) > tol(item.total):
            item.warnings.append("Qté × PU Net ≠ Total")
    if not pairs:
        return [item]

    split_total = sum(q for _, q, _ in pairs)
    out = []
    for code, q, _ in pairs:
        variant = replace(
            item,
            sku=item.sku,  # SKU exact du fournisseur, partagé par les tailles (la variante = SKU + taille)
            qty=q,
            size=decode_size(code),
            total=round(q * item.unit_price_net, 2) if item.unit_price_net is not None else None,
            warnings=list(item.warnings),
        )
        if item.qty and split_total != item.qty:
            variant.warnings.append(f"Somme des tailles ({split_total}) ≠ Qté totale ({item.qty})")
        out.append(variant)
    return out


def parse_pdf_table(pdf_bytes: bytes, mapping: Optional[dict[str, str]] = None) -> tuple[list[LineItem], TableTrace]:
    return parse_pages(extract_pages(pdf_bytes), mapping)
