"""Application des règles métier : marque, type, titre, options, prix, poids."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from .config import TAG_NEW, WEIGHT_DECK_G, WEIGHT_NORMAL_G
from .models import LineItem

SIZE_SINGLE_RE = re.compile(r"^(\d)?(XXS|XS|XXL|XL|S|M|L)$", re.I)  # 2M, 3L, 4XL -> M, L, XL
SIZE_COMPOUND_RE = re.compile(r"^(XS/S|S/M|M/L|L/XL|XL/XXL)$", re.I)  # conservés tels quels
NUMERIC_SIZE_RE = re.compile(r"^(2[6-9]|3\d|4[0-6])$")
DECK_WIDTH_RE = re.compile(r"^([6-9](?:[.,]\d{1,2})?)(?:\"|''|in)?$")
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
    """Normalise une taille ; None si ce n'est pas une taille. 2M->M, 3L->L, 4XL->XL, S/M inchangé."""
    if SIZE_COMPOUND_RE.match(token):
        return token.upper()
    m = SIZE_SINGLE_RE.match(token)
    return m.group(2).upper() if m else None


def detect_brand(description: str, brands: list[str]) -> tuple[str, str]:
    """Retourne (marque canonique, description sans la marque)."""
    for brand in sorted(brands, key=len, reverse=True):
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(brand)}(?![A-Za-z0-9])", re.I)
        if pattern.search(description):
            return brand, re.sub(r"\s{2,}", " ", pattern.sub("", description, count=1)).strip()
    return "", description


def detect_type(description: str, rules: list[dict]) -> Optional[dict]:
    low = description.lower()
    for rule in rules:
        for kw in rule["keywords"]:
            if re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", low):
                return rule
    return None


def _pop_token(tokens: list[str], predicate) -> Optional[str]:
    """Retire et retourne le dernier token satisfaisant predicate."""
    for idx in range(len(tokens) - 1, -1, -1):
        if predicate(tokens[idx]):
            return tokens.pop(idx)
    return None


def normalize_item(
    item: LineItem,
    rules: dict,
    type_mapping: dict[str, str],
    collections: dict,
    coef: float,
    tva_pct: float,
) -> Product:
    warnings = list(item.warnings)
    brand, remaining = detect_brand(item.description, rules.get("brands", []))
    if not brand:
        warnings.append("Marque non reconnue (Vendor vide)")

    rule = detect_type(remaining, rules["product_types"])
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
        if width:
            w = DECK_WIDTH_RE.match(width).group(1).replace(",", ".")
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
        numeric_ok = kind in rules.get("sized_numeric_kinds", [])
        size_tok = _pop_token(
            tokens,
            lambda t: normalize_size(t) is not None or (numeric_ok and bool(NUMERIC_SIZE_RE.match(t))),
        )
        if size_tok:
            opt.update(n1="Taille", v1=normalize_size(size_tok) or size_tok)
        color_tok = _pop_token(tokens, lambda t: t.lower() in {c.lower() for c in rules.get("colors", [])})
        if color_tok:
            tail_parts.append(color_tok.capitalize())

    core = " ".join(tokens).strip(" -")
    if core.isupper():
        core = core.title()
    if not core:
        core = item.description
        warnings.append("Nom du produit vide après nettoyage")

    head = " ".join(p for p in (brand, label) if p)
    title = " - ".join(p for p in (head, core, " - ".join(tail_parts)) if p)

    cost = item.unit_price_net if item.unit_price_net is not None else item.unit_price_gross
    if cost is None:
        warnings.append("Coût absent")
    if item.public_price is not None:
        price = round(item.public_price, 2)
    elif cost is not None:
        price = round(cost * coef * (1 + tva_pct / 100), 2)
        warnings.append("Prix public absent : prix calculé (coût × coef × TVA)")
    else:
        price = None

    allowed = collections.get("allowed", [])
    tags = [TAG_NEW]
    coll = collections.get("by_type_key", {}).get(type_key)
    if coll and coll in allowed:
        tags.append(coll)

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
