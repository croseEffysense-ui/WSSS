"""Rapprochement Bon de Commande (PO) / Bon de Livraison (BL) — 100 % déterministe."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .models import LineItem

OK_MESSAGE = "Perfect, all the articles correspond to the BL and everything matches. ✅"
KO_MESSAGE = "⚠️ There are discrepancies between the PO and BL."

STATUS_OK = "✅ OK"
STATUS_QTY = "⚠️ Écart de quantité"
STATUS_MISSING = "❌ Absent du BL"
STATUS_DUP = "⚠️ Doublon sur le PO"
STATUS_BARCODE = "⚠️ SKU différent (code-barres)"
STATUS_EXTRA = "➕ Absent du PO"


@dataclass
class ReconciliationResult:
    quantities: list[Optional[int]] = field(default_factory=list)  # ordre exact du PO
    discrepancies: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)  # tableau détaillé pour l'affichage

    @property
    def ok(self) -> bool:
        return not self.discrepancies

    @property
    def header(self) -> str:
        return OK_MESSAGE if self.ok else KO_MESSAGE

    def quantities_text(self) -> str:
        """Une valeur par ligne (copier-coller Shopify) ; '?' = ligne discordante, jamais de quantité inventée."""
        return "\n".join("?" if q is None else str(q) for q in self.quantities)


def _key(item: LineItem) -> str:
    return item.sku.strip().upper()


def reconcile(po: list[LineItem], bl: list[LineItem]) -> ReconciliationResult:
    res = ReconciliationResult()
    po_counts = Counter(_key(i) for i in po)
    bl_counts = Counter(_key(i) for i in bl)

    bl_qty: dict[str, int] = {}
    for i in bl:
        bl_qty[_key(i)] = bl_qty.get(_key(i), 0) + i.qty
    bl_by_barcode = {i.barcode: _key(i) for i in bl if i.barcode}

    for k, c in po_counts.items():
        if c > 1:
            res.discrepancies.append(f"Article en double sur le PO : {k} ({c} lignes)")
    for k, c in bl_counts.items():
        if c > 1:
            res.discrepancies.append(f"Article en double sur le BL : {k} ({c} lignes, quantités additionnées)")

    used: set[str] = set()
    for n, item in enumerate(po, start=1):
        k = _key(item)
        status = STATUS_OK
        if k not in bl_qty and item.barcode in bl_by_barcode:
            alt = bl_by_barcode[item.barcode]
            res.discrepancies.append(f"{k} : SKU différent sur le BL ({alt}), rapproché via le code-barres")
            k, status = alt, STATUS_BARCODE
        if k in used:  # doublon PO : la quantité BL n'est attribuée qu'une fois
            res.quantities.append(None)
            res.rows.append({"N°": n, "SKU": _key(item), "Qté PO": item.qty, "Qté BL": None, "Statut": STATUS_DUP})
            continue
        if k not in bl_qty:
            res.discrepancies.append(f"Présent sur le PO mais absent du BL : {_key(item)}")
            res.quantities.append(None)
            res.rows.append({"N°": n, "SKU": _key(item), "Qté PO": item.qty, "Qté BL": None, "Statut": STATUS_MISSING})
            continue
        used.add(k)
        qty = bl_qty[k]
        if item.qty and item.qty != qty:
            res.discrepancies.append(f"Quantité différente pour {k} : PO {item.qty} / BL {qty}")
            status = STATUS_QTY
        res.quantities.append(qty)
        res.rows.append({"N°": n, "SKU": _key(item), "Qté PO": item.qty, "Qté BL": qty, "Statut": status})

    for k, qty in bl_qty.items():
        if k not in used:
            res.discrepancies.append(f"Présent sur le BL mais absent du PO : {k}")
            res.rows.append({"N°": None, "SKU": k, "Qté PO": None, "Qté BL": qty, "Statut": STATUS_EXTRA})
    return res
