"""Conversion des tailles étrangères (US, UK…) en tailles françaises.

Un « tableau de correspondance » = un nom (ex. « Chaussures »), un système d'origine (ex. « US »)
et des lignes taille d'origine -> taille FR. Chaque type de produit (product_rules) peut désigner son tableau
(`size_chart`) ; un modèle fournisseur peut en donner un par défaut et dire dans quel système sont ses tailles.
Les tailles en lettres (XS, S, M, L, XL…) sont internationales : jamais converties.
Module pur (ni Streamlit ni autre module du projet) : utilisable par le stockage, la config, l'UI et les tests.
"""
from __future__ import annotations

import re
from typing import Optional

DOC_SIZES = "size_conversions.json"
NO_CONVERSION = ""  # tailles déjà françaises / européennes
SIZE_SYSTEMS = ["US", "UK"]

LETTER_SIZE_RE = re.compile(r"^(\d?X{0,3}S|M|\d?X{0,3}L|XS/S|S/M|M/L|L/XL|XL/XXL|TU|OS|ONE SIZE|U)$", re.I)
_SYSTEM_MARK_RE = re.compile(r"\b(US|UK|EU|FR)\b", re.I)
_FRACTIONS = {"1/2": 0.5, "1/3": 1 / 3, "2/3": 2 / 3, "1/4": 0.25, "3/4": 0.75, "1/8": 0.125, "3/8": 0.375, "5/8": 0.625, "7/8": 0.875}

Charts = dict[tuple[str, str], dict[str, str]]  # (tableau en minuscules, système) -> {taille normalisée: taille FR}


def size_key(value: str) -> str:
    """Forme comparable d'une taille : « US 9,5 » -> « 9.5 », « 9 1/2 » -> « 9.5 », « W30 » -> « 30 », « 7 1/8 » -> « 7.125 »."""
    s = _SYSTEM_MARK_RE.sub(" ", str(value)).upper().replace(",", ".").strip()
    s = re.sub(r"^W\s*(?=\d)", "", s)  # tour de taille « W30 »
    s = re.sub(r"\s*/\s*L?\d+$", "", s) if re.match(r"^\d{2}\s*/\s*L?\d{2}$", s) else s  # « 30/32 » (W/L) -> 30
    m = re.match(r"^(\d+(?:\.\d+)?)(?:\s+(\d/\d))?$", s)
    if m:
        return f"{float(m.group(1)) + _FRACTIONS.get(m.group(2) or '', 0):g}"
    return re.sub(r"\s+", " ", s)


def marked_system(value: str) -> Optional[str]:
    """Système écrit dans la taille elle-même : « US 9 » -> « US », « 42 EU » -> « » (déjà européenne), sinon None."""
    m = _SYSTEM_MARK_RE.search(str(value))
    if not m:
        return None
    mark = m.group(1).upper()
    return NO_CONVERSION if mark in {"EU", "FR"} else mark


def charts_from_doc(doc: dict) -> Charts:
    charts: Charts = {}
    for row in doc.get("charts", []):
        chart, system = str(row.get("chart", "")).strip(), str(row.get("system", "")).strip().upper()
        src, dst = str(row.get("from", "")).strip(), str(row.get("to", "")).strip()
        if chart and system and src and dst:
            charts.setdefault((chart.lower(), system), {})[size_key(src)] = dst
    return charts


def chart_names(doc: dict) -> list[str]:
    """Noms des tableaux (ordre d'apparition, sans doublon)."""
    seen: dict[str, str] = {}
    for row in doc.get("charts", []):
        name = str(row.get("chart", "")).strip()
        if name and name.lower() not in seen:
            seen[name.lower()] = name
    return list(seen.values())


def convert_size(value: str, system: str, chart: str, charts: Charts) -> tuple[str, Optional[str]]:
    """(taille FR, alerte éventuelle). `system` = système du document ; une marque « US 9 » / « 42 EU » dans la taille prime."""
    raw = str(value or "").strip()
    if not raw:
        return raw, None
    system = (marked_system(raw) if marked_system(raw) is not None else system or NO_CONVERSION).upper()
    if system == NO_CONVERSION or LETTER_SIZE_RE.match(_SYSTEM_MARK_RE.sub("", raw).strip()):
        return _SYSTEM_MARK_RE.sub("", raw).strip() or raw, None
    if not chart:
        return raw, f"Taille {system} « {raw} » non convertie : aucun tableau de tailles pour ce type de produit"
    table = charts.get((chart.lower(), system))
    if table is None:
        return raw, f"Taille {system} « {raw} » non convertie : le tableau « {chart} » n'a pas de lignes {system}"
    converted = table.get(size_key(raw))
    if converted is None:
        return raw, f"Taille {system} « {raw} » absente du tableau « {chart} » : ajoutez-la (🛠️ Configuration → Tailles)"
    return converted, None


def validate_charts(rows: list[dict]) -> list[str]:
    """Erreurs du tableau de correspondances (liste vide = valide)."""
    errors: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    for n, r in enumerate(rows, start=1):
        chart, system = str(r.get("chart", "")).strip(), str(r.get("system", "")).strip().upper()
        src, dst = str(r.get("from", "")).strip(), str(r.get("to", "")).strip()
        if not (chart and system and src and dst):
            errors.append(f"Ligne {n} : tableau, système, taille d'origine et taille FR sont requis")
            continue
        if system not in SIZE_SYSTEMS:
            errors.append(f"Ligne {n} : système « {system} » inconnu (permis : {', '.join(SIZE_SYSTEMS)})")
        key = (chart.lower(), system, size_key(src))
        if key in seen:
            errors.append(f"Ligne {n} : taille {system} « {src} » en double dans « {chart} »")
        seen.add(key)
    return errors
