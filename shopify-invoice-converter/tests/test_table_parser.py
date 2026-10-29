"""Lecture par colonnes (positions des mots), modèles par en-têtes et analyse complète — sans vrai PDF :
les pages sont construites à partir de mots positionnés (x en points, ~5 pt par caractère)."""
import pytest

from converter.table_parser import Line, Word, group_lines, parse_amount, parse_pages

CHAR = 5.0


def L(page, top, *cols, h=8.0):
    """Ligne synthétique : L(1, 100, ("Qté", 300), ("7", 306)) ; un tuple peut contenir plusieurs mots séparés d'un espace."""
    words = []
    for text, x in cols:
        for token in text.split(" "):
            words.append(Word(token, x, x + CHAR * len(token), top, top + h))
            x += CHAR * (len(token) + 1)
    return Line(page, words)


# Facture « type Adidas » : 11 colonnes, en-tête sur 3 lignes, tailles « 40 / 1 » parasites, nombres alignés à droite
ADIDAS = [
    L(1, 10, ("FACTURE adidas France", 300)),
    L(1, 154, ("Numéro", 60), ("Quantité", 294), ("Prix", 487), ("Remise", 532)),
    L(1, 159, ("Position", 17), ("Description de l'Article", 155), ("Taille / Quantité", 371), ("Prix Net", 581),
      ("Montant Net", 630), ("Montant TVA", 700), ("Montant Total", 770)),
    L(1, 164, ("d'Article", 59), ("Totale", 297), ("Catalogue", 476), ("Totale %", 529)),
    L(1, 181, ("Commande client : 1006301752", 20)),
    L(1, 196, ("1", 33), ("LC1068", 62), ("STAN SMITH ADV X VINCENT MILOU FTWW", 103), ("7", 306), ("3913 / 1", 334),
      ("40 / 1", 364), ("50,05", 493), ("8,00", 553), ("46,05", 594), ("322,34", 655), ("64,47", 730), ("350,35", 805)),
    L(1, 215, ("7", 306), ("Sous-total avant remise", 680), ("350,35", 805)),
]


def test_parse_amount_formats():
    assert parse_amount("1.050,30") == 1050.30 and parse_amount("€2223.76") == 2223.76
    assert parse_amount("28,01-") == -28.01 and parse_amount("20,00%") == 20.0 and parse_amount("2,00") == 2.0
    assert parse_amount("1,234.5") == 1234.5 and parse_amount("12") == 12.0
    assert parse_amount("LC1068") is None and parse_amount("Qté") is None


def test_eleven_columns_multiline_header_auto():
    items, trace = parse_pages([ADIDAS])
    fields = {c.label: c.field for c in trace.columns}
    assert fields["Numéro d'Article"] == "sku" and fields["Quantité Totale"] == "qty" and fields["Prix Catalogue"] == "unit_price_gross"
    assert fields["Prix Net"] == "unit_price_net" and fields["Montant Net"] == "total" and fields["Montant Total"] is None
    assert len(items) == 1
    it = items[0]
    assert (it.sku, it.qty, it.unit_price_gross, it.unit_price_net, it.total) == ("LC1068", 7, 50.05, 46.05, 322.34)
    assert it.description == "STAN SMITH ADV X VINCENT MILOU FTWW" and not it.warnings
    roles = {lt.text.split()[0]: lt.role for lt in trace.lines}
    assert roles["Commande"] == "ignorée" and roles["7"] == "pied de tableau"


def test_template_mapping_exact_label_beats_partial():
    # « Prix » (modèle) ne doit pas capter « Prix Net » : correspondance exacte d'abord
    page = [
        L(1, 10, ("Ref", 20), ("Designation", 100), ("Qte", 250), ("Prix Net", 300), ("Prix", 380), ("Mt", 440)),
        L(1, 30, ("AB123", 20), ("Tee shirt", 100), ("2", 255), ("9,00", 300), ("10,00", 380), ("18,00", 440)),
    ]
    mapping = {"sku": "Ref", "description": "Designation", "qty": "Qte", "unit_price_gross": "Prix", "unit_price_net": "Prix Net", "total": "Mt"}
    items, _ = parse_pages([page], mapping)
    assert (items[0].unit_price_gross, items[0].unit_price_net, items[0].total) == (10.0, 9.0, 18.0)
    assert parse_pages([page])[0][0].total is None  # « Mt » n'est pas un libellé connu : seul le modèle le lit


def test_description_above_sku_and_page_top_continuation():
    header = L(1, 50, ("Code", 20), ("Description", 120), ("Qty", 330), ("Price", 380), ("Total", 440))
    p1 = [
        header,
        L(1, 80, ("Santa Cruz T-Shirt", 120)),  # au-dessus de la référence, après un blanc
        L(1, 89, ("SCA-TEE-12915", 20), ("Opus Black XS", 120), ("2", 335), ("10.87", 380), ("21.74", 440)),
        L(1, 98, ("ADULT", 120)),  # collée dessous
        L(1, 130, ("INTR006", 20), ("INDEPENDENT", 120), ("4", 335), ("19.00", 380), ("76.00", 440)),
        L(1, 800, ("Page 1/2", 300)),
    ]
    p2 = [
        L(2, 20, ("TRUCK POLISHED 149", 120)),  # haut de page : suite de l'article précédent
        L(2, 29, ("659641857865", 20)),
        L(2, 60, ("Total: €97.74", 300)),
    ]
    items, trace = parse_pages([p1, p2])
    assert [i.sku for i in items] == ["SCA-TEE-12915", "INTR006"]
    assert items[0].description == "Santa Cruz T-Shirt Opus Black XS ADULT"
    assert items[1].description == "INDEPENDENT TRUCK POLISHED 149" and items[1].barcode == "659641857865"
    assert any(lt.role == "pied de tableau" for lt in trace.lines)


def test_inline_sku_and_discount_gives_net_cost():
    page = [
        L(1, 10, ("Product / Service", 65), ("Quantity", 447), ("Price", 494), ("Remise", 535), ("Total", 590)),
        L(1, 40, ("1 PM - Black - M08", 65)),
        L(1, 49, ("SKU: F11000410", 65), ("4", 447), ("50.00", 494), ("10%", 540), ("180.00", 585)),
        L(1, 58, ("HS Code: 640399", 65)),
    ]
    items, _ = parse_pages([page])
    it = items[0]
    assert it.sku == "F11000410" and it.qty == 4 and it.description == "1 PM - Black - M08"
    assert it.unit_price_gross == 50.0 and it.unit_price_net == 45.0 and not it.warnings  # 180 / 4


def test_no_header_gives_readable_problem():
    items, trace = parse_pages([[L(1, 10, ("Bonjour", 10)), L(1, 30, ("AB-CD-1 x 5 1", 10))]])
    assert items == [] and "en-tête" in trace.problem


def test_group_lines_merges_small_font_offsets():
    words = [Word("3913", 334, 351, 192, 200), Word("STAN", 103, 121, 195, 204), Word("LC1068", 62, 87, 197, 206)]
    lines = group_lines(words, 1)
    assert len(lines) == 1 and lines[0].text == "LC1068 STAN 3913"


# ----------------------------- Modèles par en-têtes (config) -----------------------------
def test_table_text_roundtrip_and_validation():
    from converter.formats import table_from_text, table_to_text, validate_table
    table = {"sku": "Numéro d'Article", "qty": "Quantité Totale", "unit_price_net": "Prix Net"}
    text = table_to_text(table)
    assert text == "SKU / Référence = Numéro d'Article ; Qté = Quantité Totale ; PU Net = Prix Net"
    assert table_from_text(text) == table and table_from_text("quantité = Qty ; pu ht = Price") == {"qty": "Qty", "unit_price_gross": "Price"}
    assert validate_table("A", table) == []
    assert any("Qté" in e for e in validate_table("A", {"sku": "Ref"}))
    assert any("SKU" in e for e in validate_table("A", {"qty": "Qté"}))
    assert any("plusieurs" in e for e in validate_table("A", {"sku": "X", "qty": "x"}))
    assert any("inconnu" in e for e in validate_table("A", {"sku": "X", "qty": "Y", "bidule": "Z"}))


def test_formats_from_doc_splits_kinds_and_upsert():
    from converter.formats import formats_from_doc, upsert_template
    doc = {"formats": [
        {"name": "Simple", "columns": ["qty", "total"]},
        {"name": "Adidas", "table": {"sku": "Article", "qty": "Quantité"}, "match": "adidas"},
        {"name": "Cassé", "table": {"sku": "Article"}},
    ]}
    profiles, templates, problems = formats_from_doc(doc)
    assert profiles == {"Simple": ["qty", "total"]} and templates == {"Adidas": {"table": {"sku": "Article", "qty": "Quantité"}, "match": "adidas"}}
    assert len(problems) == 1 and "Cassé" in problems[0]
    new = upsert_template(doc, "adidas", {"sku": "Réf", "qty": "Qté"}, "adidas France")
    assert [f["name"] for f in new["formats"]] == ["Simple", "Cassé", "adidas"] and new["formats"][-1]["match"] == "adidas France"


def test_shipped_adidas_template_is_valid():
    from converter.config import load_formats
    profiles, templates, problems = load_formats()
    assert "Adidas" in templates and not problems and len(profiles) == 4


def test_sheets_template_row_and_old_three_column_sheet():
    from test_pipeline import FakeGateway, _local
    from converter.storage import SheetsStore
    gw = FakeGateway()
    store = SheetsStore(gw, seed=_local())
    doc = store.load("supplier_formats.json")
    assert next(f for f in doc["formats"] if f["name"] == "Adidas")["table"]["qty"] == "Quantité Totale"
    assert gw.sheets["supplier_formats"][0] == ["name", "columns", "notes", "match", "table"]
    gw.sheets["supplier_formats"] = [["name", "columns", "notes"], ["Vans", "Qté, Total", ""]]  # Sheet créé avant la mise à jour
    store.refresh()
    assert store.load("supplier_formats.json") == {"formats": [{"name": "Vans", "columns": ["qty", "total"], "notes": ""}]}


def test_validate_templates_table():
    import pandas as pd
    from converter.config_ui import validate_templates
    from converter.formats import TABLE_FIELDS
    lab = TABLE_FIELDS
    ok, errs = validate_templates(pd.DataFrame([
        {"name": "Adidas", "match": "adidas", lab["sku"]: "Article", lab["qty"]: "Quantité", "notes": ""},
        {"name": None, "match": None, lab["sku"]: None, lab["qty"]: None, "notes": None},
    ]))
    assert not errs and ok == [{"name": "Adidas", "table": {"sku": "Article", "qty": "Quantité"}, "match": "adidas", "notes": ""}]
    _, errs = validate_templates(pd.DataFrame([{"name": "X", lab["sku"]: "Article"}]))
    assert len(errs) == 1


# ----------------------------- Analyse complète -----------------------------
@pytest.fixture
def fake_pdf(monkeypatch):
    from converter import analysis
    monkeypatch.setattr(analysis, "extract_pages", lambda _: [ADIDAS])
    monkeypatch.setattr(analysis, "extract_text", lambda _: "\n".join(line.text for line in ADIDAS))
    return analysis


def test_analyze_prefers_recognised_supplier_template(fake_pdf):
    from converter.config import load_formats
    profiles, templates, _ = load_formats()
    a = fake_pdf.analyze(b"%PDF", fake_pdf.AUTO, profiles, templates)
    assert not a.error and a.chosen.name == "Adidas" and a.chosen.supplier_match
    assert a.items[0].sku == "LC1068" and "Qté = « Quantité Totale »" in a.chosen.describe()
    assert {c.method for c in a.candidates} == {fake_pdf.METHOD_TEMPLATE, fake_pdf.METHOD_AUTO_TABLE, fake_pdf.METHOD_TAIL}


def test_analyze_auto_table_without_any_saved_format(fake_pdf):
    a = fake_pdf.analyze(b"%PDF", fake_pdf.AUTO, {}, {})
    assert a.chosen.name == fake_pdf.AUTO_TABLE and len(a.items) == 1


def test_analyze_explicit_choice_errors(fake_pdf):
    a = fake_pdf.analyze(b"%PDF", "Bon de commande (Qté, PU)", {"Bon de commande (Qté, PU)": ["qty", "unit_price_net"]}, {})
    assert "Aucun article détecté avec ce format" in a.error and a.table_trace is not None  # le debug garde la trace auto
    assert "n'existe plus" in fake_pdf.analyze(b"%PDF", "Inconnu", {}, {}).error
