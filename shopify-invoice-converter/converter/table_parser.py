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
from dataclasses import dataclass, field
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
}

NUMBER_RE = re.compile(r"^[€$£]?-?\d+(?:[ .,]\d{3})*(?:[.,]\d+)?-?%?€?$")
BARCODE_RE = re.compile(r"^(\d{8}|\d{12,14})$")  # EAN-8, UPC-12, EAN-13, GTIN-14
SKU_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{2,}$")
INLINE_SKU_RE = re.compile(r"^(sku|ref|r[ée]f|r[ée]f[ée]rence|art|article|item|code)[.:]?$", re.I)
FOOTER_RE = re.compile(
    r"^(sous[- ]?total|total|montant total|montant ht|net [aà] payer|subtotal|tva|vat|report|a reporter)\b", re.I
)
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
        targets = {fld: normalize_label(label) for fld, label in mapping.items() if normalize_label(label)}
        # libellés identiques d'abord, puis approchants : « Prix » ne doit pas capter la colonne « Prix Net »
        for exact in (True, False):
            for fld, target in targets.items():
                if any(c.field == fld for c in columns):
                    continue
                for c in columns:
                    lab = normalize_label(c.label)
                    if c.field is None and (lab == target if exact else (target in lab or lab in target)):
                        c.field = fld
                        break
        return
    for fld, patterns in HEADER_SYNONYMS.items():
        for pattern in patterns:
            hit = next((c for c in columns if c.field is None and re.search(pattern, normalize_label(c.label))), None)
            if hit is not None:
                hit.field = fld
                break


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
    """Colonnes (champs + zones) d'un en-tête choisi à la main (formulaire « Créer un modèle » du mode debug)."""
    columns = _merge_header(lines)
    assign_fields(columns, mapping)
    _set_zones(columns)
    return columns


def _header_score(columns: list[Column]) -> int:
    """Nombre de champs reconnus ; 0 si l'essentiel manque (Qté + référence ou désignation)."""
    fields = {c.field for c in columns if c.field}
    return len(fields) if "qty" in fields and fields & {"sku", "description"} else 0


def find_header(lines: list[Line], mapping: Optional[dict[str, str]] = None) -> Optional[tuple[int, int, list[Column]]]:
    """Cherche le bloc de 1 à 3 lignes (sans chiffres, rapprochées) qui ressemble le plus à un en-tête de tableau.

    Retourne (index de début, index de fin exclu, colonnes avec champs et zones) ou None.
    """
    best: Optional[tuple[tuple[int, int, int], int, int, list[Column]]] = None
    targets = {k: normalize_label(v) for k, v in (mapping or {}).items()}
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
            exact = sum(1 for c in columns if c.field and targets.get(c.field) == normalize_label(c.label))
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


def _numeric(line: Line, fld: str, columns: list[Column]) -> Optional[Word]:
    """Nombre de la ligne rangé sous la colonne `fld` : chaque nombre va à l'en-tête le plus proche,
    et si plusieurs nombres tombent sous la même colonne (tailles « 40 / 1 »…), le plus proche gagne."""
    best: Optional[tuple[float, Word]] = None
    for w in line.words:
        if parse_amount(w.text) is None or BARCODE_RE.match(w.text):
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
        for ln in pending:
            if page_top and rows:
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
            sku, qty = _sku_word(line, fields), _numeric(line, "qty", columns)
            if not (sku and qty) and _is_footer(line):
                lt.role, lt.detail = ROLE_FOOTER, "fin du tableau (total / sous-total)"
                flush(pending, not page_has_item)
                inside = False
            elif NOISE_RE.search(line.text):
                lt.role, lt.detail = ROLE_IGNORED, "ligne d'information (n° commande, douane, origine, page…)"
            elif sku and qty:
                lt.role, lt.detail = ROLE_ITEM, f"SKU {sku.text} · Qté {qty.text}"
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
    items = [_build_item(anchor, extra, columns, trace) for anchor, extra in rows]
    if not items:
        trace.problem = "En-tête trouvé, mais aucune ligne ne contient à la fois une référence et une quantité sous ces colonnes."
    return items, trace


def _build_item(anchor: Line, extra: list[Line], columns: list[Column], trace: TableTrace) -> LineItem:
    fields = by_field(columns)
    sku_col, desc_col = fields.get("sku"), fields.get("description")
    sku_word = _sku_word(anchor, fields)
    item = LineItem(sku=sku_word.text if sku_word else "")
    used: set[int] = {id(sku_word)}
    if sku_word is not None and (sku_col is None or not sku_col.holds(sku_word)):  # « SKU: F1100 » dans la désignation
        i = anchor.words.index(sku_word)
        used.add(id(anchor.words[i - 1]))

    for fld in NUMERIC_FIELDS:
        if fld not in fields:
            continue
        word = _numeric(anchor, fld, columns) or next((w for ln in extra if (w := _numeric(ln, fld, columns))), None)
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

    desc: list[str] = []
    for ln in sorted([anchor, *extra], key=lambda l: (l.page, l.top)):
        # sur la ligne article, la désignation déborde souvent sous l'en-tête Référence ; pas sur les lignes de suite
        zones = [c for c in (desc_col, sku_col if (ln is anchor or desc_col is None) else None) if c is not None]
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
    trace.cells.append({c.label: " ".join(w.text for w in anchor.words if c.holds(w)) for c in columns})
    if item.qty <= 0:
        item.warnings.append("Quantité absente ou nulle")
    if item.qty and item.unit_price_net is not None and item.total is not None:
        if abs(item.qty * item.unit_price_net - item.total) > tol(item.total):
            item.warnings.append("Qté × PU Net ≠ Total")
    return item


def parse_pdf_table(pdf_bytes: bytes, mapping: Optional[dict[str, str]] = None) -> tuple[list[LineItem], TableTrace]:
    return parse_pages(extract_pages(pdf_bytes), mapping)
