"""Extraction des lignes articles depuis un PDF texte (facture, BL, bon de commande).

Approche : on découpe le texte en « blocs » (un bloc = un SKU + ses lignes de
description + sa queue numérique), puis on mappe les nombres de fin de bloc sur
un profil de colonnes (format fournisseur, voir formats.py et config/supplier_formats.json). Tolère : décimales virgule/point,
symbole €, quantité seule sur une ligne, description multiligne, en-têtes de page
répétés, EAN/Gencod dans le bloc.
"""
from __future__ import annotations

import re
from typing import BinaryIO, Optional

import pypdf
from pypdf.errors import PdfReadError

from .models import LineItem

# SKU : tokens avec au moins un tiret, une lettre et un chiffre (exclut dates et n° de téléphone)
SKU_RE = re.compile(r"^(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{2,}(?:-[A-Za-z0-9]+)+$")
BARCODE_RE = re.compile(r"^\d{8,14}$")
NUMBER_RE = re.compile(r"^\d+(?:[.,]\d+)?%?$")
IGNORE_RE = re.compile(
    r"^(page\b|\d+\s*/\s*\d+$|order\b|commande\b|bon de\b|facture\b|invoice\b|date\b|"
    r"d[ée]signation|r[ée]f[ée]rence|ref\b|qt[ée]\b|quantit[ée]|sku\b|description\b|prix\b|p\.?u\.?\b)",
    re.I,
)
FOOTER_RE = re.compile(
    r"^(total|sous[- ]?total|net [àa] payer|tva\b|montant|iban|bic\b|conditions|merci|vat\b|subtotal)", re.I
)


class PdfExtractionError(Exception):
    """Erreur lisible (affichable telle quelle dans Streamlit)."""


def extract_text(pdf_file: BinaryIO) -> str:
    """Extrait le texte de toutes les pages ; essaie le mode 'layout' puis le mode standard."""
    try:
        reader = pypdf.PdfReader(pdf_file)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise PdfExtractionError("Le PDF est protégé par mot de passe.") from exc
        pages = []
        for page in reader.pages:
            try:
                text = page.extract_text(extraction_mode="layout")
            except TypeError:  # anciennes versions de pypdf
                text = page.extract_text()
            if not text or not text.strip():
                text = page.extract_text() or ""
            pages.append(text)
    except PdfExtractionError:
        raise
    except (PdfReadError, ValueError, OSError) as exc:
        raise PdfExtractionError(f"PDF illisible ou corrompu : {exc}") from exc

    full = "\n".join(pages).strip()
    if not full:
        raise PdfExtractionError(
            "Aucun texte trouvé : le PDF est probablement un scan/une image. "
            "Passez-le d'abord par un OCR (ou utilisez un PDF texte)."
        )
    return full


def parse_number(token: str) -> float:
    """'1,5' -> 1.5 ; '10.87' -> 10.87 ; '20%' -> 20.0."""
    return float(token.rstrip("%").replace(",", "."))


def _is_number(token: str) -> bool:
    return bool(NUMBER_RE.match(token)) and not BARCODE_RE.match(token)


def _parse_block(sku: str, lines: list[str], fields: list[str]) -> LineItem:
    """Transforme les lignes d'un bloc en LineItem."""
    texts: list[str] = []
    numbers: list[str] = []
    barcode = ""
    n = len(fields)

    for line in lines:
        tokens = [t for t in line.replace("\u00a0", " ").split() if t not in {"€", "EUR", "$"}]
        rest = []
        for t in tokens:
            if not barcode and BARCODE_RE.match(t):
                barcode = t
            else:
                rest.append(t)
        if len(numbers) >= n:  # colonnes déjà complètes : le reste est de la description
            texts.extend(rest)
            continue
        run: list[str] = []
        while rest and _is_number(rest[-1]):
            run.insert(0, rest.pop())
        room = n - len(numbers)
        if not rest:  # ligne purement numérique
            numbers.extend(run[-room:])
        elif len(run) >= 2:  # queue numérique d'une ligne mixte
            numbers.extend(run[-room:])
            texts.extend(rest + run[:-room] if len(run) > room else rest)
        else:  # 1 seul nombre en fin de texte : probablement partie de la description
            texts.extend(rest + run)

    item = LineItem(sku=sku.replace(" ", ""), description=" ".join(texts).strip(), barcode=barcode)

    if len(numbers) < n:
        item.warnings.append(f"{len(numbers)}/{n} colonnes numériques trouvées")
    values = numbers[-n:] if len(numbers) >= n else numbers
    for field_name, raw in zip(fields, values):
        try:
            val = parse_number(raw)
        except ValueError:
            item.warnings.append(f"Nombre illisible : {raw}")
            continue
        if field_name == "qty":
            if val != int(val):
                item.warnings.append(f"Quantité non entière : {raw}")
            item.qty = int(val)
        else:
            setattr(item, field_name, val)

    if "qty" not in fields[: len(values)] or item.qty <= 0:
        item.warnings.append("Quantité absente ou nulle")
    if item.qty and item.unit_price_net is not None and item.total is not None:
        if abs(item.qty * item.unit_price_net - item.total) > 0.05:
            item.warnings.append("Qté × PU Net ≠ Total")
    return item


def parse_text(text: str, fields: list[str]) -> list[LineItem]:
    """Parse le texte brut d'un document en liste de LineItem."""
    blocks: list[tuple[str, list[str]]] = []
    current: Optional[tuple[str, list[str]]] = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        first, _, remainder = line.partition(" ")
        if SKU_RE.match(first) and not IGNORE_RE.match(line):
            current = (first, [remainder.strip()])
            blocks.append(current)
        elif FOOTER_RE.match(line):
            current = None
        elif current is not None and not IGNORE_RE.match(line):
            current[1].append(line)

    return [_parse_block(sku, lines, fields) for sku, lines in blocks]


def parse_pdf(pdf_file: BinaryIO, fields: list[str]) -> list[LineItem]:
    """Point d'entrée : PDF -> LineItem. Lève PdfExtractionError si le fichier est inexploitable."""
    items = parse_text(extract_text(pdf_file), fields)
    if not items:
        raise PdfExtractionError(
            "Aucun article détecté. Vérifiez le profil de colonnes choisi ou le format des SKU "
            "(attendu : AAA-BBB-1234)."
        )
    return items


def detect_profile(text: str, profiles: dict[str, list[str]]) -> tuple[str, list[LineItem]]:
    """Teste chaque profil de colonnes et retient celui qui donne le plus de lignes sans alerte.

    Départage : à score égal, le profil avec le plus de colonnes (le plus précis) gagne.
    Retourne (nom du profil, lignes). Lève PdfExtractionError si aucun article n'est détecté.
    """
    best: Optional[tuple[tuple[int, int], str, list[LineItem]]] = None
    for name, fields in profiles.items():
        items = parse_text(text, fields)
        if not items:
            continue
        score = (sum(1 for i in items if not i.warnings), len(fields))
        if best is None or score > best[0]:
            best = (score, name, items)
    if best is None:
        raise PdfExtractionError(
            "Aucun article détecté. Vérifiez que le document est bien une facture / un BL "
            "dont les références ont la forme AAA-BBB-1234."
        )
    return best[1], best[2]
