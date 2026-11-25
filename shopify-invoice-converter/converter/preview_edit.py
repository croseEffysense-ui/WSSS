"""Aperçu éditable de l'étape 3 : listes déroulantes basées sur la configuration, puis retour au format CSV.

Module pur (aucune dépendance à Streamlit) : `to_editable` convertit le DataFrame Shopify (tout en texte) en types
adaptés à l'éditeur (nombres, listes de tags, vide = None pour les listes déroulantes) ; `from_editable` fait
le chemin inverse pour que le CSV exporté garde exactement le format du template.
"""
from __future__ import annotations

import math
from typing import Iterable

import pandas as pd

from .config import TAG_NEW

# Valeurs imposées par Shopify (non configurables) : propositions des listes déroulantes
BOOL_COLUMNS = ["Published on online store", "Charge tax", "Requires shipping", "Gift card"]
ENUM_COLUMNS: dict[str, list[str]] = {
    "Status": ["active", "draft", "archived"],
    "Continue selling when out of stock": ["DENY", "CONTINUE"],
    "Weight unit for display": ["g", "kg", "lb", "oz"],
    "Inventory tracker": ["shopify"],
    "Fulfillment service": ["manual"],
    **{c: ["TRUE", "FALSE"] for c in BOOL_COLUMNS},
}
OPTION_NAMES = ["Taille", "Largeur", "Dureté", "Diamètre", "Couleur"]
LETTER_SIZES = ["XS", "S", "M", "L", "XL", "XXL", "XS/S", "S/M", "M/L", "L/XL", "XL/XXL", "JR/XS", "TU"]
NUMBER_COLUMNS = {"Price": float, "Cost per item": float, "Inventory quantity": int, "Weight value (grams)": int}
DISABLED_COLUMNS = ["URL handle"]  # dérivé du titre et partagé par les variantes : non modifiable ici
SELECT_COLUMNS = ["Vendor", "Type", "Option1 name", "Option1 value", "Option2 name", "Option2 value", *ENUM_COLUMNS]


def _uniq(values: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for v in values:
        v = str(v).strip()
        if v:
            seen.setdefault(v, None)
    return list(seen)


def split_tags(value) -> list[str]:
    return _uniq(str(value or "").split(","))


def dropdown_options(df: pd.DataFrame, rules: dict, type_mapping: dict, collections: dict, size_charts: dict) -> dict[str, list[str]]:
    """Propositions par colonne : la configuration d'abord, puis les valeurs déjà présentes (jamais perdues)."""
    def present(col: str) -> list[str]:
        return _uniq(df[col]) if col in df.columns else []

    chart_targets = _uniq(fr for chart in size_charts.values() for fr in chart.values())
    sizes = _uniq([*LETTER_SIZES, *chart_targets])
    opts = {
        "Vendor": _uniq([*rules.get("brands", []), *present("Vendor")]),
        "Type": _uniq([*(v for k, v in type_mapping.items() if not k.startswith("_")), *present("Type")]),
        "Option1 name": _uniq([*OPTION_NAMES, *present("Option1 name")]),
        "Option2 name": _uniq([*OPTION_NAMES, *present("Option2 name")]),
        "Option1 value": _uniq([*sizes, *present("Option1 value")]),
        "Option2 value": _uniq([*present("Option2 value")]),
        "Tags": _uniq([TAG_NEW, *collections.get("allowed", []), *(t for v in present("Tags") for t in split_tags(v))]),
    }
    for col, values in ENUM_COLUMNS.items():
        opts[col] = _uniq([*values, *present(col)])
    return opts


def to_editable(df: pd.DataFrame) -> pd.DataFrame:
    """Copie typée pour l'éditeur : nombres, tags en liste, cellules vides des listes déroulantes = None."""
    out = df.copy()
    for col in SELECT_COLUMNS:
        if col in out.columns:
            out[col] = out[col].map(lambda v: (str(v).strip() or None) if v is not None and not _is_nan(v) else None).astype(object)
    for col, kind in NUMBER_COLUMNS.items():
        if col in out.columns:
            nums = pd.to_numeric(out[col].map(lambda v: None if v is None or str(v).strip() == "" else v), errors="coerce")
            out[col] = nums.astype("Int64") if kind is int else nums.astype(float)
    if "Tags" in out.columns:
        out["Tags"] = out["Tags"].map(split_tags).astype(object)
    return out


def _is_nan(v) -> bool:
    return isinstance(v, float) and math.isnan(v)


def _join_tags(v) -> str:
    if v is None or _is_nan(v):
        return ""
    return str(v) if isinstance(v, str) else ", ".join(_uniq(v))


def from_editable(edited: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Retour au format CSV Shopify : tout en texte, prix à 2 décimales, quantités entières, tags séparés par « , »."""
    out = edited.copy()
    for col in out.columns:
        if col in NUMBER_COLUMNS:
            kind = NUMBER_COLUMNS[col]
            def fmt(v, kind=kind, col=col):
                if v is None or pd.isna(v):
                    return ""
                return int(round(float(v))) if kind is int else (f"{float(v):.2f}" if col in ("Price", "Cost per item") else v)
            out[col] = out[col].map(fmt).astype(object)
        elif col == "Tags":
            out[col] = out[col].map(_join_tags).astype(object)
        else:
            out[col] = out[col].map(lambda v: "" if v is None or _is_nan(v) or v is pd.NA else str(v)).astype(object)
    return out[[c for c in columns if c in out.columns]].reset_index(drop=True)
