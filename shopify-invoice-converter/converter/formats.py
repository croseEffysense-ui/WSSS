"""Formats de factures par fournisseur.

Deux sortes de formats, enregistrés dans le même document (supplier_formats) :
- « colonnes de fin de ligne » : un nom + la liste ordonnée des colonnes numériques lues de gauche à droite
  à la fin de chaque ligne article (factures simples dont les références ont la forme AAA-BBB-1234) ;
- « modèle par en-têtes » : un nom + {champ: libellé de l'en-tête de colonne sur la facture}
  (ex. Qté = « Quantité Totale », PU Net = « Prix Net »), lu par position (voir table_parser.py).
  Fonctionne quel que soit le nombre de colonnes de la facture. `match` = texte présent sur les factures
  de ce fournisseur (nom, n° de TVA…) pour que la détection automatique le reconnaisse.
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

# champs d'un modèle par en-têtes (clé interne -> libellé affiché)
TABLE_FIELDS: dict[str, str] = {
    "sku": "SKU / Référence",
    "description": "Description",
    **FIELD_LABELS,
}

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
_TABLE_ALIASES: dict[str, str] = {
    **_ALIASES,
    "sku": "sku", "sku reference": "sku", "reference": "sku", "ref": "sku",
    "description": "description", "designation": "description",
}


def normalize_label(text: str) -> str:
    """Minuscules, sans accents ni ponctuation : « PU HT (brut) » -> « pu ht brut »."""
    s = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


_normalize = normalize_label


def field_key(text: str) -> Optional[str]:
    """Clé interne d'une colonne à partir de sa clé, de son libellé ou d'un alias ; None si inconnue."""
    return _ALIASES.get(_normalize(text))


def table_field_key(text: str) -> Optional[str]:
    """Comme field_key, en acceptant aussi SKU / Référence et Description."""
    return _TABLE_ALIASES.get(_normalize(text))


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


def table_from_text(text: str) -> dict[str, str]:
    """« Qté = Quantité Totale ; PU Net = Prix Net » -> {'qty': 'Quantité Totale', 'unit_price_net': 'Prix Net'}.

    Les champs non reconnus sont conservés tels quels pour être signalés par la validation.
    """
    out: dict[str, str] = {}
    for part in str(text or "").split(";"):
        key, sep, label = part.partition("=")
        if sep and key.strip() and label.strip():
            out[table_field_key(key) or key.strip().lower()] = label.strip()
    return out


def table_to_text(table: dict[str, str]) -> str:
    """{'qty': 'Quantité Totale'} -> « Qté = Quantité Totale » (lisible et modifiable dans un tableur)."""
    return " ; ".join(f"{TABLE_FIELDS.get(k, k)} = {v}" for k, v in table.items() if str(v).strip())


def validate_format(name: str, columns: list[str]) -> list[str]:
    """Erreurs d'un format « colonnes de fin de ligne » (liste vide = valide)."""
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


def validate_table(name: str, table: dict[str, str]) -> list[str]:
    """Erreurs d'un modèle par en-têtes (liste vide = valide)."""
    errors: list[str] = []
    if not name.strip():
        errors.append("nom du fournisseur requis")
    unknown = [k for k in table if k not in TABLE_FIELDS]
    if unknown:
        errors.append(f"champ(s) inconnu(s) : {', '.join(unknown)} (permis : {', '.join(TABLE_FIELDS.values())})")
    if not str(table.get("qty", "")).strip():
        errors.append("l'en-tête de la colonne « Qté » est indispensable")
    if not (str(table.get("sku", "")).strip() or str(table.get("description", "")).strip()):
        errors.append("indiquez l'en-tête de la colonne « SKU / Référence » (ou au moins « Description »)")
    labels = [normalize_label(v) for v in table.values() if str(v).strip()]
    dupes = sorted({v for v in labels if labels.count(v) > 1})
    if dupes:
        errors.append(f"même en-tête utilisé pour plusieurs champs : {', '.join(dupes)}")
    return errors


def formats_from_doc(doc: dict) -> tuple[dict[str, list[str]], dict[str, dict], list[str]]:
    """Document de config -> (formats « fin de ligne » {nom: colonnes}, modèles par en-têtes {nom: modèle}, problèmes).

    Un modèle = {"table": {champ: libellé d'en-tête}, "match": texte qui identifie le fournisseur}.
    Les formats invalides sont ignorés (et signalés) plutôt que de bloquer toute l'application.
    """
    profiles: dict[str, list[str]] = {}
    templates: dict[str, dict] = {}
    problems: list[str] = []
    seen: set[str] = set()
    for n, entry in enumerate(doc.get("formats", []), start=1):
        name = str(entry.get("name") or "").strip()
        label = f"« {name} »" if name else f"n°{n}"
        table = {str(k): str(v) for k, v in (entry.get("table") or {}).items() if str(v).strip()}
        if table:
            errors = validate_table(name, table)
        else:
            columns = [str(c) for c in entry.get("columns", [])]
            errors = validate_format(name, columns)
        if not errors and name.lower() in seen:
            errors.append("nom en double")
        if errors:
            problems.append(f"Format {label} ignoré : {' ; '.join(errors)}")
            continue
        seen.add(name.lower())
        if table:
            templates[name] = {"table": table, "match": str(entry.get("match") or "").strip()}
        else:
            profiles[name] = columns
    return profiles, templates, problems


def profiles_from_doc(doc: dict) -> tuple[dict[str, list[str]], list[str]]:
    """Formats « colonnes de fin de ligne » utilisables + problèmes rencontrés (tous formats confondus)."""
    profiles, _, problems = formats_from_doc(doc)
    return profiles, problems


def upsert_template(doc: dict, name: str, table: dict[str, str], match: str = "", notes: str = "") -> dict:
    """Ajoute (ou remplace, même nom sans tenir compte de la casse) un modèle par en-têtes ; retourne le nouveau doc."""
    entry = {"name": name.strip(), "table": {k: v for k, v in table.items() if str(v).strip()}, "match": match.strip(), "notes": notes}
    formats = [f for f in doc.get("formats", []) if str(f.get("name", "")).strip().lower() != name.strip().lower()]
    return {**doc, "formats": [*formats, entry]}
