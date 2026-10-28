"""Formats de factures par fournisseur : colonnes numériques en fin de ligne article.

Un « format » = un nom (fournisseur) + la liste ordonnée des colonnes numériques lues de gauche à droite.
Module pur (ni Streamlit ni autre module du projet) : utilisable par le stockage, la config, l'UI et les tests.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional

# clé interne (attribut de LineItem) -> libellé affiché
FIELD_LABELS: dict[str, str] = {
    "qty": "Qté",
    "unit_price_gross": "PU HT (brut)",
    "unit_price_net": "PU Net",
    "total": "Total",
    "public_price": "Prix public TTC",
}
MAX_COLUMNS = len(FIELD_LABELS)

# Variantes acceptées quand le format est saisi à la main (ex. dans Google Sheets)
_ALIASES: dict[str, str] = {
    "qty": "qty", "qte": "qty", "quantite": "qty", "quantity": "qty",
    "unit price gross": "unit_price_gross", "pu ht": "unit_price_gross", "pu ht brut": "unit_price_gross",
    "pu brut": "unit_price_gross",
    "unit price net": "unit_price_net", "pu net": "unit_price_net", "pu": "unit_price_net",
    "prix net": "unit_price_net",
    "total": "total", "total ht": "total", "montant": "total",
    "public price": "public_price", "prix public": "public_price", "prix public ttc": "public_price",
}


def _normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation : « PU HT (brut) » -> « pu ht brut »."""
    s = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def field_key(text: str) -> Optional[str]:
    """Clé interne d'une colonne à partir de sa clé, de son libellé ou d'un alias ; None si inconnue."""
    return _ALIASES.get(_normalize(text))


def columns_from_text(text: str) -> list[str]:
    """« Qté, PU Net, Total » -> ['qty', 'unit_price_net', 'total'].

    Les jetons non reconnus sont conservés tels quels (en minuscules) pour que la validation puisse les signaler
    au lieu de les perdre silencieusement.
    """
    out: list[str] = []
    for token in str(text or "").split(","):
        token = token.strip()
        if token:
            out.append(field_key(token) or token.lower())
    return out


def columns_to_text(columns: list[str]) -> str:
    """['qty', 'total'] -> « Qté, Total » (lisible dans un tableur)."""
    return ", ".join(FIELD_LABELS.get(c, c) for c in columns)


def validate_format(name: str, columns: list[str]) -> list[str]:
    """Erreurs d'un format (liste vide = valide)."""
    errors: list[str] = []
    if not name.strip():
        errors.append("nom du fournisseur requis")
    if not columns:
        errors.append("au moins une colonne requise")
        return errors
    unknown = [c for c in columns if c not in FIELD_LABELS]
    if unknown:
        errors.append(f"colonne(s) inconnue(s) : {', '.join(unknown)} (permises : {', '.join(FIELD_LABELS.values())})")
    dupes = sorted({c for c in columns if columns.count(c) > 1})
    if dupes:
        errors.append(f"colonne(s) en double : {', '.join(FIELD_LABELS.get(c, c) for c in dupes)}")
    if "qty" not in columns:
        errors.append("la colonne « Qté » est indispensable")
    return errors


def profiles_from_doc(doc: dict) -> tuple[dict[str, list[str]], list[str]]:
    """Document de config -> (profils utilisables {nom: colonnes}, problèmes rencontrés).

    Les formats invalides sont ignorés (et signalés) plutôt que de bloquer toute l'application.
    """
    profiles: dict[str, list[str]] = {}
    problems: list[str] = []
    seen: set[str] = set()
    for n, entry in enumerate(doc.get("formats", []), start=1):
        name = str(entry.get("name") or "").strip()
        columns = [str(c) for c in entry.get("columns", [])]
        label = f"« {name} »" if name else f"n°{n}"
        errors = validate_format(name, columns)
        if not errors and name.lower() in seen:
            errors.append("nom en double")
        if errors:
            problems.append(f"Format {label} ignoré : {' ; '.join(errors)}")
            continue
        seen.add(name.lower())
        profiles[name] = columns
    return profiles, problems
