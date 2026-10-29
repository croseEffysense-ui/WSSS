"""Analyse complète d'un PDF : essaie toutes les méthodes de lecture, retient la plus cohérente et garde
la trace de tout (texte, positions, formats testés) pour le mode debug.

Méthodes, de la plus spécifique à la plus générale :
1. modèles par en-têtes enregistrés (un par fournisseur, reconnu grâce à son texte `match`) ;
2. en-têtes de tableau reconnus automatiquement (« Quantité », « Prix Net »…) ;
3. formats « colonnes de fin de ligne » (références AAA-BBB-1234, méthode historique).
Module sans Streamlit : testable isolément.
"""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Optional

from .formats import FIELD_LABELS, TABLE_FIELDS, normalize_label
from .models import LineItem
from .pdf_parser import PdfExtractionError, extract_text, parse_text
from .table_parser import Line, TableTrace, extract_pages, pages_text, parse_pages

AUTO = "🔍 Détection automatique"
AUTO_TABLE = "📋 Tableau (en-têtes reconnus automatiquement)"
METHOD_TEMPLATE = "Modèle par en-têtes"
METHOD_AUTO_TABLE = "En-têtes reconnus automatiquement"
METHOD_TAIL = "Colonnes de fin de ligne"
_METHOD_RANK = {METHOD_TEMPLATE: 2, METHOD_AUTO_TABLE: 1, METHOD_TAIL: 0}


@dataclass
class Candidate:
    """Résultat d'une méthode de lecture."""

    name: str
    method: str
    items: list[LineItem] = field(default_factory=list)
    trace: Optional[TableTrace] = None
    columns: list[str] = field(default_factory=list)  # formats « fin de ligne »
    supplier_match: bool = False  # le texte `match` du modèle figure dans le PDF

    @property
    def ok(self) -> int:
        return sum(1 for i in self.items if not i.warnings)

    @property
    def score(self) -> tuple[int, int, int, int]:
        """Fournisseur reconnu > lignes sans alerte > méthode la plus fiable > nombre de lignes.

        Un modèle dont le fournisseur n'est pas reconnu passe après la lecture automatique : à résultat égal,
        une facture Vans ne doit pas être présentée comme lue « avec le modèle Adidas ».
        """
        rank = _METHOD_RANK[self.method] if self.method != METHOD_TEMPLATE or self.supplier_match else 0
        return int(self.supplier_match), self.ok, rank, len(self.items)

    def describe(self) -> str:
        if self.method == METHOD_TAIL:
            return " → ".join(FIELD_LABELS[c] for c in self.columns)
        cols = self.trace.columns if self.trace else []
        return ", ".join(f"{TABLE_FIELDS[c.field]} = « {c.label} »" for c in cols if c.field) or "aucune colonne reconnue"


@dataclass
class Analysis:
    text: str = ""
    text_source: str = ""
    pages: list[list[Line]] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    chosen: Optional[Candidate] = None
    error: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def items(self) -> list[LineItem]:
        return self.chosen.items if self.chosen else []

    @property
    def table_trace(self) -> Optional[TableTrace]:
        """Trace par colonnes la plus utile au debug : celle du format retenu, sinon la détection automatique."""
        if self.chosen and self.chosen.trace:
            return self.chosen.trace
        auto = next((c for c in self.candidates if c.method == METHOD_AUTO_TABLE), None)
        return auto.trace if auto else None


def analyze(
    pdf_bytes: bytes,
    choice: str = AUTO,
    profiles: Optional[dict[str, list[str]]] = None,
    templates: Optional[dict[str, dict]] = None,
) -> Analysis:
    """Lit le PDF avec toutes les méthodes disponibles. Ne lève pas d'exception : voir `Analysis.error`."""
    profiles, templates = profiles or {}, templates or {}
    a = Analysis()
    try:
        a.pages = extract_pages(pdf_bytes)
    except PdfExtractionError as exc:
        a.notes.append(f"Positions des mots indisponibles (lecture par colonnes impossible) : {exc}")
    fallback = pages_text(a.pages).strip()
    try:
        a.text, a.text_source = extract_text(io.BytesIO(pdf_bytes)), "pypdf (mode mise en page)"
        if len(fallback) > 2 * len(a.text.replace(" ", "")):  # pypdf passe parfois à côté de presque tout le texte
            a.text, a.text_source = fallback, "pdfplumber (pypdf n'a lu qu'une petite partie du texte)"
    except PdfExtractionError as exc:
        if not fallback:
            a.error = str(exc)
            return a
        a.text, a.text_source = fallback, "pdfplumber (pypdf n'a pas trouvé de texte)"
        a.notes.append(f"pypdf : {exc}")

    norm_text = normalize_label(a.text)
    if a.pages:
        for name, tpl in templates.items():
            items, trace = parse_pages(a.pages, tpl["table"])
            match = bool(tpl.get("match")) and normalize_label(tpl["match"]) in norm_text
            a.candidates.append(Candidate(name, METHOD_TEMPLATE, items, trace, supplier_match=match))
        items, trace = parse_pages(a.pages)
        a.candidates.append(Candidate(AUTO_TABLE, METHOD_AUTO_TABLE, items, trace))
    for name, columns in profiles.items():
        a.candidates.append(Candidate(name, METHOD_TAIL, parse_text(a.text, columns), columns=columns))

    if choice == AUTO:
        pool = [c for c in a.candidates if c.items]
        if pool:
            a.chosen = max(pool, key=lambda c: c.score)
        else:
            a.error = (
                "Aucun article détecté : ni tableau avec en-têtes (Référence, Quantité…) reconnu, ni référence de la "
                "forme AAA-BBB-1234. Activez 🐞 Mode debug (barre latérale) pour voir ce que contient le PDF et "
                "enregistrer un modèle pour ce fournisseur."
            )
        return a
    a.chosen = next((c for c in a.candidates if c.name == choice), None)
    if a.chosen is None:
        a.error = f"Le format « {choice} » n'existe plus : choisissez-en un autre."
    elif not a.chosen.items:
        a.error = "Aucun article détecté avec ce format. Essayez un autre format ou la détection automatique."
    return a
