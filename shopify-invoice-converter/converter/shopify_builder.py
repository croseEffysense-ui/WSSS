"""Construction du DataFrame final au format du template Shopify."""
from __future__ import annotations

from collections import OrderedDict

import pandas as pd

from .config import WEIGHT_UNIT
from .normalizer import Product


def consolidate(products: list[Product]) -> list[Product]:
    """Somme les quantités des lignes identiques (même handle + SKU + options). Ordre conservé."""
    merged: "OrderedDict[tuple, Product]" = OrderedDict()
    for p in products:
        key = (p.handle, p.sku, p.option1_value, p.option2_value)
        if key in merged:
            merged[key].qty += p.qty
            merged[key].warnings.append("Lignes identiques consolidées")
        else:
            merged[key] = p
    return list(merged.values())


def build_dataframe(products: list[Product], columns: list[str]) -> pd.DataFrame:
    """Génère les lignes CSV : colonnes et ordre strictement identiques au template.

    - 1ère ligne d'un produit : toutes les infos produit.
    - Lignes suivantes : Title/Vendor/Type/Tags vides ; handle + noms/valeurs d'options répétés.
    """
    rows: list[dict] = []
    seen_handles: set[str] = set()

    for p in consolidate(products):
        row = {col: "" for col in columns}
        first = p.handle not in seen_handles
        seen_handles.add(p.handle)

        values = {
            "URL handle": p.handle,
            "SKU": p.sku,
            "Barcode": p.barcode,
            "Option1 name": p.option1_name,
            "Option1 value": p.option1_value,
            "Option2 name": p.option2_name,
            "Option2 value": p.option2_value,
            "Price": f"{p.price:.2f}" if p.price is not None else "",
            "Cost per item": f"{p.cost:.2f}" if p.cost is not None else "",
            "Charge tax": "TRUE",
            "Inventory tracker": "shopify",
            "Inventory quantity": p.qty,
            "Continue selling when out of stock": "DENY",
            "Weight value (grams)": p.weight_g,
            "Weight unit for display": WEIGHT_UNIT,
            "Requires shipping": "TRUE",
            "Fulfillment service": "manual",
        }
        if first:
            values.update(
                {
                    "Title": p.title,
                    "Vendor": p.vendor,
                    "Type": p.product_type,
                    "Tags": p.tags,
                    "Published on online store": "FALSE",  # canaux : Whatnot + POS uniquement
                    "Status": "Active",
                    "Gift card": "FALSE",
                }
            )
        for key, val in values.items():
            if key in row:
                row[key] = val
        rows.append(row)

    return pd.DataFrame(rows, columns=columns)
