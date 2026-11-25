"""Non-régression sur les VRAIES factures fournisseurs (dossier « Factures fournisseurs »).

Chaque facture doit être lue sans alerte, avec le bon nombre d'unités et un total égal à celui imprimé sur la facture.
Ces tests sont ignorés si le dossier n'existe pas (ex. intégration continue) ; variable INVOICES_DIR pour un autre dossier.
Indépendants de la configuration métier : ils ne contrôlent que l'extraction.
"""
import os
from pathlib import Path

import pytest

from converter.analysis import AUTO, analyze
from converter.config import CONFIG_DIR, load_formats

ROOT = Path(__file__).resolve().parents[2]
INVOICES = Path(os.environ.get("INVOICES_DIR", ROOT / "Factures fournisseurs"))

# facture -> (lignes, unités, total HT imprimé sur la facture, tolérance d'arrondi, modèle attendu : chaque facture doit être reconnue par le n° de TVA de son fournisseur)
EXPECTED = {
    "Adidas": (6, 7, 322.34, 0.02, "Adidas"),
    "Carhartt": (8, 13, 430.34, 0.05, "Carhartt WIP"),
    "Encore": (21, 31, 1395.00, 0.02, "Encore"),
    "Santa Cruz": (55, 108, 2223.76, 0.02, "Santa Cruz (Shiner)"),  # sans la ligne « Delivery Charge » (0 €)
    "V7": (9, 31, 560.40, 0.02, "V7"),
    "Vans": (5, 30, 1050.30, 0.02, "Vans (VF France)"),
    "VPM": (45, 50, 2718.00, 0.02, "VPM"),  # 2 838 € facturés − 120 € de frais de port (« Shipment »)
}


@pytest.fixture(scope="module")
def formats():
    profiles, templates, _ = load_formats()
    return profiles, templates


@pytest.mark.skipif(not INVOICES.is_dir(), reason="dossier des factures fournisseurs absent")
@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_invoice_is_read_correctly(name, formats):
    pdf = INVOICES / f"{name}.pdf"
    if not pdf.exists():
        pytest.skip(f"{pdf.name} absent")
    lines, units, total, tol, fmt = EXPECTED[name]
    a = analyze(pdf.read_bytes(), AUTO, *formats)
    assert not a.error, a.error
    assert len(a.items) == lines and sum(i.qty for i in a.items) == units
    assert [i for i in a.items if i.warnings] == [], [(i.sku, i.warnings) for i in a.items if i.warnings][:3]
    assert abs(sum(i.total for i in a.items) - total) <= tol
    assert a.chosen.name == fmt and a.chosen.supplier_match  # reconnue par l'identifiant (TVA), pas par simple lecture des tableaux
    # et aucun AUTRE modèle ne doit se croire reconnu
    assert [c.name for c in a.candidates if c.supplier_match] == [fmt]
    declared = a.declared_units
    assert declared is None or declared == units  # le total d'unités imprimé, s'il existe, doit concorder
