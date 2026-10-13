"""Modèles de données partagés entre les modules."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class LineItem:
    """Une ligne brute extraite d'une facture / BL / bon de commande."""

    sku: str
    description: str = ""
    qty: int = 0
    unit_price_gross: Optional[float] = None  # PU HT avant remise
    unit_price_net: Optional[float] = None  # PU Net après remise
    total: Optional[float] = None
    public_price: Optional[float] = None  # Prix public TTC
    barcode: str = ""  # Gencod / EAN (toujours conservé en texte)
    warnings: list[str] = field(default_factory=list)


EDITOR_COLUMNS = {
    "sku": "SKU",
    "description": "Description",
    "qty": "Qté",
    "unit_price_gross": "PU HT (brut)",
    "unit_price_net": "PU Net",
    "total": "Total",
    "public_price": "Prix public TTC",
    "barcode": "Barcode",
}


def items_to_df(items: list[LineItem]) -> pd.DataFrame:
    """Convertit des LineItem en DataFrame éditable (+ colonne Alertes)."""
    rows = []
    for it in items:
        row = {label: getattr(it, attr) for attr, label in EDITOR_COLUMNS.items()}
        row["Alertes"] = " | ".join(it.warnings)
        rows.append(row)
    return pd.DataFrame(rows, columns=[*EDITOR_COLUMNS.values(), "Alertes"])


def _clean(value, cast=None, default=None):
    if value is None or (isinstance(value, float) and pd.isna(value)) or value is pd.NA:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return cast(value) if cast else value


def df_to_items(df: pd.DataFrame) -> list[LineItem]:
    """Reconstruit des LineItem depuis le DataFrame édité par l'utilisateur."""
    items: list[LineItem] = []
    for _, r in df.iterrows():
        sku = str(_clean(r.get("SKU"), str, "")).strip()
        if not sku:
            continue
        items.append(
            LineItem(
                sku=sku,
                description=str(_clean(r.get("Description"), str, "")),
                qty=_clean(r.get("Qté"), lambda v: int(float(v)), 0),
                unit_price_gross=_clean(r.get("PU HT (brut)"), float),
                unit_price_net=_clean(r.get("PU Net"), float),
                total=_clean(r.get("Total"), float),
                public_price=_clean(r.get("Prix public TTC"), float),
                barcode=str(_clean(r.get("Barcode"), str, "")).strip(),
                warnings=[w for w in str(_clean(r.get("Alertes"), str, "")).split(" | ") if w],
            )
        )
    return items
