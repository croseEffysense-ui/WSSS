"""Application des règles métier : marque, type, titre, options, prix, poids, collections."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from .config import TAG_NEW, WEIGHT_DECK_G, WEIGHT_NORMAL_G
from .models import LineItem
from .sizes import Charts, convert_size

SIZE_SINGLE_RE = re.compile(r"^(\d)?(XXS|XS|XXL|XL|S|M|L)$", re.I)  # 2M, 3L, 4XL -> M, L, XL
SIZE_COMPOUND_RE = re.compile(r"^(XS/S|S/M|M/L|L/XL|XL/XXL)$", re.I)  # conservés tels quels
NUMERIC_SIZE_RE = re.compile(r"^(2[6-9]|3\d|4[0-6])$")
SHOE_CODE_RE = re.compile(r"^([MW])0?(\d{1,2}(?:[.,]5)?)$", re.I)  # pointures codées : M08 -> 8, M09.5 -> 9.5, W07 -> W7
DECK_WIDTH_RE = re.compile(r"^([6-9](?:[.,]\d{1,3})?)(?:\"|''|in)?$")
WHEEL_HARDNESS_RE = re.compile(r"^(\d{2,3}[AD])$", re.I)
WHEEL_DIAMETER_RE = re.compile(r"^(\d{2,3}mm)$", re.I)


@dataclass
class Product:
    """Une ligne de variante prête à être convertie en ligne CSV Shopify."""

    title: str
    handle: str
    vendor: str
    product_type: str
    type_key: str
    kind: str
    tags: str
    sku: str
    barcode: str
    price: Optional[float]
    cost: Optional[float]
    qty: int
    weight_g: int
    option1_name: str = ""
    option1_value: str = ""
    option2_name: str = ""
    option2_value: str = ""
    warnings: list[str] = field(default_factory=list)


def slugify(text: str) -> str:
    """Handle Shopify : minuscules, sans accents, tirets."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def normalize_size(token: str) -> Optional[str]:
    """Normalise une taille ; None si ce n'est pas une taille. 2M->M, 3L->L, 4XL->XL, S/M inchangé, M08->8."""
    if SIZE_COMPOUND_RE.match(token):
        return token.upper()
    m = SIZE_SINGLE_RE.match(token)
    if m:
        return m.group(2).upper()
    m = SHOE_CODE_RE.match(token)
    if m and 3 <= float(m.group(2).replace(",", ".")) <= 16:  # « M65 » (nom de veste) n'est pas une pointure
        size = m.group(2).replace(",", ".")
        return size if m.group(1).upper() == "M" else f"W{size}"
    return None


def deck_width(raw: str) -> str:
    """Largeur de board au 1/8 de pouce le plus proche : 8.0 -> 8, 8.06 -> 8, 8.25, 8.375, 8.5 inchangés."""
    value = round(float(raw.replace(",", ".")) * 8) / 8
    return f"{value:g}"


def _phrase_re(phrase: str) -> re.Pattern:
    """Expression en mot entier ; « (EU) » est aussi trouvé collé : « T-Shirt(EU) »."""
    before = r"(?<![A-Za-z0-9])" if phrase[:1].isalnum() else ""
    after = r"(?![A-Za-z0-9])" if phrase[-1:].isalnum() else ""
    return re.compile(before + re.escape(phrase) + after, re.I)


def _clean(text: str) -> str:
    """Espaces doubles, tirets orphelins (« Black - - Suede ») et parenthèses vides retirés."""
    text = re.sub(r"\(\s*\)", " ", text)
    text = re.sub(r"(\s*-\s*){2,}", " - ", text)
    return re.sub(r"\s{2,}", " ", text).strip(" -/")


def detect_brand(description: str, brands: list[str]) -> tuple[str, str]:
    """Retourne (marque canonique, description sans la marque)."""
    for brand in sorted(brands, key=len, reverse=True):
        pattern = _phrase_re(brand)
        if pattern.search(description):
            return brand, _clean(pattern.sub(" ", description, count=1))
    return "", description


def detect_type(description: str, rules: list[dict]) -> tuple[Optional[dict], str]:
    """Retourne (règle, description sans AUCUN des mots-clés du type) pour éviter « T-Shirt … T-Shirt ».

    Un mot-clé précédé de « + » (ex. « +stan smith ») fait reconnaître le type SANS être retiré du titre :
    pour les noms de modèles, qui sont une information à conserver.
    """
    for rule in rules:
        keywords = sorted(rule["keywords"], key=lambda k: len(k.lstrip("+")), reverse=True)
        if any(_phrase_re(kw.lstrip("+").strip()).search(description) for kw in keywords if kw.lstrip("+").strip()):
            for kw in keywords:
                if not kw.startswith("+") and kw.strip():
                    description = _phrase_re(kw.strip()).sub(" ", description)
            return rule, _clean(description)
    return None, description


def detect_color(description: str, colors: list[str]) -> tuple[str, str]:
    """Couleur la plus à droite de la désignation (expressions de plusieurs mots acceptées : « Black Dirty Wash »).

    Retourne (couleur telle qu'écrite dans la liste, description sans la couleur).
    """
    best: Optional[tuple[int, int, str, re.Match]] = None
    for color in colors:
        for m in _phrase_re(color).finditer(description):
            key = (m.end(), len(color))
            if best is None or key > best[:2]:
                best = (m.end(), len(color), color, m)
    if best is None:
        return "", description
    m = best[3]
    return best[2], _clean(description[: m.start()] + " " + description[m.end():])


def remove_words(description: str, words: list[str]) -> str:
    """Retire les mots parasites des titres (« (EU) », « ADULT »…)."""
    for word in sorted((w for w in words if w.strip()), key=len, reverse=True):
        description = _phrase_re(word.strip()).sub(" ", description)
    return _clean(description)


def _pop_token(tokens: list[str], predicate) -> Optional[str]:
    """Retire et retourne le dernier token satisfaisant predicate."""
    for idx in range(len(tokens) - 1, -1, -1):
        if predicate(tokens[idx]):
            return tokens.pop(idx)
    return None


def as_list(value) -> list[str]:
    """Collection(s) associée(s) : texte seul (ancien format) ou liste."""
    if not value:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def product_tags(type_key: str, brand: str, collections: dict) -> tuple[list[str], list[str]]:
    """(tags, alertes) : « nouveautés » + collections du type + collections de la marque, uniquement celles autorisées."""
    allowed = set(collections.get("allowed", []))
    wanted = as_list(collections.get("by_type_key", {}).get(type_key)) if type_key else []
    by_brand = {k.lower(): v for k, v in collections.get("by_brand", {}).items()}
    wanted += as_list(by_brand.get(brand.lower())) if brand else []
    tags, warnings = [TAG_NEW], []
    for coll in wanted:
        if coll not in allowed:
            continue
        if "," in coll:  # Shopify sépare les tags par des virgules : la collection serait coupée en deux
            warnings.append(f"Collection « {coll} » non ajoutée : son nom contient une virgule (impossible en tag)")
        elif coll not in tags:
            tags.append(coll)
    return tags, warnings


def normalize_item(
    item: LineItem,
    rules: dict,
    type_mapping: dict[str, str],
    collections: dict,
    coef: float,
    tva_pct: float,
    size_system: str = "",
    size_chart: str = "",
    charts: Optional[Charts] = None,
    fallback_price: bool = False,
    default_vendor: str = "",
    default_type: str = "",
) -> Product:
    """Ligne de facture -> variante Shopify.

    `size_system` = système des tailles du document (« US », « UK » ; vide = déjà FR/EU) ; la taille est alors
    convertie avec le tableau du type de produit (`size_chart` de la règle), à défaut `size_chart` (celui du modèle).
    `fallback_price` : sans prix public TTC sur la facture, calculer un prix (coût × coef × TVA). Désactivé par
    défaut : un prix absent de la facture reste vide (jamais inventé).
    `default_vendor` / `default_type` (réglages du modèle fournisseur) : utilisés seulement si la désignation ne
    contient ni marque ni type reconnus (ex. fournisseur qui est lui-même la marque, fournisseur de chaussures).
    """
    warnings = list(item.warnings)
    description = remove_words(item.description, rules.get("title_ignore", []))
    brand, remaining = detect_brand(description, rules.get("brands", []))
    if not brand and default_vendor:
        brand = default_vendor
    elif not brand:
        warnings.append("Marque non reconnue (Vendor vide)")

    rule, remaining = detect_type(remaining, rules["product_types"])
    if rule is None and default_type:
        rule = next((r for r in rules["product_types"] if r["type_key"] == default_type), None)
    kind = rule["kind"] if rule else "other"
    type_key = rule["type_key"] if rule else ""
    label = rule["label"] if rule else ""
    if not rule:
        warnings.append("Type de produit non reconnu")
    product_type = type_mapping.get(type_key, "") if type_key else ""
    if rule and not product_type:
        warnings.append(f"Pas de correspondance Product Type pour « {type_key} »")

    tokens = remaining.split()
    opt = {"n1": "", "v1": "", "n2": "", "v2": ""}
    tail_parts: list[str] = []

    if kind == "deck":
        width = _pop_token(tokens, lambda t: bool(DECK_WIDTH_RE.match(t)))
        # « 8.125 X 31.7 » : la longueur suit la largeur ; seule la largeur est gardée (en variante et en fin de titre)
        tokens = _clean(re.sub(r"(?<![A-Za-z0-9])[xX]\s*\d{2}(?:[.,]\d+)?(?:\"|in)?(?![A-Za-z0-9])", " ", " ".join(tokens))).split()
        if width:
            w = deck_width(DECK_WIDTH_RE.match(width).group(1))
            opt.update(n1="Largeur", v1=w)
            tail_parts.append(w)
        else:
            warnings.append("Largeur de deck non détectée")
    elif kind == "wheel":
        hard = _pop_token(tokens, lambda t: bool(WHEEL_HARDNESS_RE.match(t)))
        diam = _pop_token(tokens, lambda t: bool(WHEEL_DIAMETER_RE.match(t)))
        if hard and diam:
            opt.update(n1="Dureté", v1=hard.upper(), n2="Diamètre", v2=diam.lower())
            tail_parts += [hard.upper(), diam.lower()]
        else:
            warnings.append("Dureté / diamètre de roue non détectés")
    else:
        if item.size:  # taille donnée par la facture (colonne « Taille / Quantité ») : pas de devinette
            opt.update(n1="Taille", v1=item.size)
        else:
            numeric_ok = kind in rules.get("sized_numeric_kinds", [])
            size_tok = _pop_token(
                tokens,
                lambda t: normalize_size(t) is not None or (numeric_ok and bool(NUMERIC_SIZE_RE.match(t))),
            )
            if size_tok:
                opt.update(n1="Taille", v1=normalize_size(size_tok) or size_tok)
        if opt["v1"]:  # taille étrangère (US…) -> équivalent français
            chart = (rule or {}).get("size_chart") or size_chart
            opt["v1"], problem = convert_size(opt["v1"], size_system, chart, charts or {})
            if problem:
                warnings.append(problem)
        color, rest = detect_color(" ".join(tokens), rules.get("colors", []))
        if color:
            tokens = rest.split()
            tail_parts.append(color)

    core = _clean(" ".join(tokens))
    if core.isupper():
        core = core.title()
    if not core:
        warnings.append("Nom de l'article absent de la désignation (titre sans nom)")

    head = " ".join(p for p in (brand, label) if p)
    title = " - ".join(p for p in (head, core, " - ".join(tail_parts)) if p)

    cost = item.unit_price_net if item.unit_price_net is not None else item.unit_price_gross
    if cost is None:
        warnings.append("Coût absent")
    if item.public_price is not None:
        price = round(item.public_price, 2)
    elif fallback_price and cost is not None:
        price = round(cost * coef * (1 + tva_pct / 100), 2)
        warnings.append("Prix public absent : prix calculé (coût × coef × TVA)")
    else:
        price = None
        warnings.append("Prix public absent de la facture : prix de vente laissé vide")

    tags, tag_warnings = product_tags(type_key, brand, collections)
    warnings += tag_warnings

    return Product(
        title=title,
        handle=slugify(title),
        vendor=brand,
        product_type=product_type,
        type_key=type_key,
        kind=kind,
        tags=", ".join(tags),
        sku=item.sku,
        barcode=item.barcode,
        price=price,
        cost=round(cost, 2) if cost is not None else None,
        qty=item.qty,
        weight_g=WEIGHT_DECK_G if kind == "deck" else WEIGHT_NORMAL_G,
        option1_name=opt["n1"],
        option1_value=opt["v1"],
        option2_name=opt["n2"],
        option2_value=opt["v2"],
        warnings=warnings,
    )
