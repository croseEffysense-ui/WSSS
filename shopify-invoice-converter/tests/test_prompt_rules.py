"""Règles du prompt « Shopify Product CSV — Master Prompt » (exemples repris du prompt)."""
from converter.config import template_columns
from converter.models import LineItem
from converter.normalizer import deck_width, normalize_item
from converter.shopify_builder import build_dataframe

RULES = {
    "brands": ["Wasted Paris", "Baker", "Obey", "Spitfire", "Etnies", "Santa Cruz", "Erased"],
    "colors": ["Black", "Sky Multi", "Black Dirty Wash", "White"],
    "product_types": [
        {"keywords": ["t-shirt", "tee"], "type_key": "tee", "kind": "apparel", "label": "T-Shirt"},
        {"keywords": ["board", "deck"], "type_key": "deck", "kind": "deck", "label": "Board"},
        {"keywords": ["wheels", "wheel"], "type_key": "wheels", "kind": "wheel", "label": "Wheels"},
        {"keywords": ["woven shirt", "shirt", "chemise"], "type_key": "shirt", "kind": "apparel", "label": "Chemise"},
        {"keywords": ["shoes", "shoe"], "type_key": "shoes", "kind": "other", "label": ""},
        {"keywords": ["belt"], "type_key": "belt", "kind": "other", "label": "Belt"},
    ],
    "sized_numeric_kinds": ["apparel"],
    "title_ignore": ["(EU)", "ADULT"],
}
MAPPING = {"tee": "T-Shirt", "deck": "Board", "wheels": "Roues", "shirt": "Chemises", "shoes": "Chaussures", "belt": "Ceintures"}
COLL = {
    "allowed": ["T-Shirts", "Santa Cruz", "Nouveautés", "Protection de Skate (Genou, Poigné, etc)"],
    "by_type_key": {"tee": ["T-Shirts", "Protection de Skate (Genou, Poigné, etc)"]},
    "by_brand": {"Santa Cruz": "Santa Cruz"},
}


def norm(description, **kw):
    item = LineItem(sku=kw.pop("sku", "SKU1"), description=description, qty=kw.pop("qty", 1), unit_price_net=kw.pop("cost", 10.0), **kw)
    return normalize_item(item, RULES, MAPPING, COLL, 2.4, 20)


def test_titles_follow_brand_type_name_color_format():
    # autres types : MARQUE - Type - Nom - Couleur
    assert norm("Obey Woven Shirt Alan Woven Sky Multi").title == "Obey - Chemise - Alan Woven - Sky Multi"
    # roues : MARQUE Wheels - Nom - DIAMÈTRE - DURETÉ (les options Shopify gardent « 56mm » / « 99D »)
    assert norm("Spitfire Wheels Conical Full Wallstreet 99D 56mm").title == "Spitfire Wheels - Conical Full Wallstreet - 56MM - 99D"
    assert norm("Etnies Shoes Kingpin Black Dirty Wash").title == "Etnies - Kingpin - Black Dirty Wash"  # libellé de type vide : omis
    # planches : MARQUE Board - Nom - Couleur (la largeur n'est qu'une option)
    assert norm("Baker Board Stop And Think 8.5").title == "Baker Board - Stop And Think"
    assert norm("Baker Board Stop And Think Black 8.5").title == "Baker Board - Stop And Think - Black"
    # mots parasites et mot-clé du type répété retirés : « T-Shirt(EU) … T-Shirt … ADULT »
    p = norm("Santa Cruz T-Shirt(EU) Burnt Opus OVRSZD T-Shirt Black XS ADULT")
    assert p.title == "Santa Cruz - T-Shirt - Burnt Opus OVRSZD - Black" and p.option1_value == "XS"


def test_board_width_variant_rounding():
    assert [deck_width(w) for w in ("8.0", "8.06", "8.25", "8.375", "8.5", "8,125")] == ["8", "8", "8.25", "8.375", "8.5", "8.125"]
    p = norm("Santa Cruz Deck Opus Color 7-Ply Birch 8.125 X 31.7")
    assert (p.option1_name, p.option1_value, p.weight_g) == ("Largeur", "8.125", 1000)


def test_clothing_size_codes_and_shoe_size_codes():
    assert norm("Wasted Paris T-Shirt Skive Black 2M").option1_value == "M"
    assert norm("Wasted Paris T-Shirt Skive Black S/M").option1_value == "S/M"
    a, b = norm("Etnies Shoes Kingpin - M08 - Black"), norm("Etnies Shoes Kingpin - M09.5 - Black")
    assert a.handle == b.handle and (a.option1_value, b.option1_value) == ("8", "9.5")  # même produit, 2 pointures
    assert norm("Erased Belt Leather Belt Black").option1_name == ""  # pas de taille : pas de variante


def test_price_never_invented_and_status_active():
    p = norm("Wasted Paris T-Shirt Skive Black M")
    assert p.price is None and any("laissé vide" in w for w in p.warnings)
    item = LineItem(sku="X", description="Wasted Paris T-Shirt Skive Black M", qty=1, unit_price_net=10.0)
    assert normalize_item(item, RULES, MAPPING, COLL, 2.4, 20, fallback_price=True).price == 28.8  # option explicite
    assert norm("Wasted Paris T-Shirt Skive Black M", public_price=29.9).price == 29.9


def test_tags_type_and_brand_collections_comma_names_skipped():
    p = norm("Santa Cruz T-Shirt Screaming Hand Black M")
    assert p.tags == "nouveautés, T-Shirts, Santa Cruz" and p.product_type == "T-Shirt"
    assert any("virgule" in w for w in p.warnings)


def test_wasted_paris_multi_variant_csv_rows():
    """Exemple du prompt : 1 produit, 4 lignes, handle et nom d'option sur CHAQUE ligne, SKU exact partagé."""
    items = [LineItem(sku="WP-TEE-1", description=f"Wasted Paris T-Shirt - Skive - Black {s}", qty=q, unit_price_net=12.0, public_price=35.0)
             for s, q in (("XS", 1), ("S", 2), ("M", 3), ("L", 1))]
    items.append(LineItem(sku="WP-TEE-1", description="Wasted Paris T-Shirt - Skive - Black M", qty=2, unit_price_net=12.0, public_price=35.0))
    df = build_dataframe([normalize_item(i, RULES, MAPPING, COLL, 2.4, 20) for i in items], template_columns())
    assert len(df) == 4 and set(df["URL handle"]) == {"wasted-paris-t-shirt-skive-black"}
    assert list(df["Title"]) == ["Wasted Paris - T-Shirt - Skive - Black", "", "", ""]
    assert list(df["Option1 name"]) == ["Taille"] * 4 and list(df["Option1 value"]) == ["XS", "S", "M", "L"]
    assert list(df["Inventory quantity"]) == [1, 2, 5, 1]  # même SKU + même taille : quantités additionnées
    assert set(df["SKU"]) == {"WP-TEE-1"} and df["Status"].iloc[0] == "active"
    assert set(df["Inventory tracker"]) == {"shopify"} and set(df["Fulfillment service"]) == {"manual"}
    assert set(df["Continue selling when out of stock"]) == {"DENY"} and df["Published on online store"].iloc[0] == "FALSE"
    assert list(df.columns) == template_columns()


def test_model_keywords_with_plus_identify_type_but_stay_in_title():
    rules = {**RULES, "product_types": [{"keywords": ["shoes", "+stan smith"], "type_key": "shoes", "kind": "other", "label": ""}]}
    p = normalize_item(LineItem(sku="X", description="Adidas Stan Smith Cloud White", qty=1, unit_price_net=40),
                       {**rules, "brands": ["Adidas"]}, MAPPING, COLL, 2.4, 20)
    assert p.product_type == "Chaussures" and p.title == "Adidas - Stan Smith Cloud - White"


def test_supplier_defaults_only_when_designation_says_nothing():
    stan = LineItem(sku="LC1068", description="STAN SMITH ADV X VINCENT MILOU FTWW", qty=1, unit_price_net=46.05, size="42")
    p = normalize_item(stan, RULES, MAPPING, COLL, 2.4, 20, default_vendor="Adidas", default_type="shoes")
    assert (p.vendor, p.product_type, p.option1_value) == ("Adidas", "Chaussures", "42")
    assert p.title == "Adidas - Stan Smith Adv X Vincent Milou Ftww" and not any("Marque" in w for w in p.warnings)
    tee = LineItem(sku="X", description="Santa Cruz T-Shirt Dot Black M", qty=1, unit_price_net=10)
    p = normalize_item(tee, RULES, MAPPING, COLL, 2.4, 20, default_vendor="Adidas", default_type="shoes")
    assert (p.vendor, p.product_type) == ("Santa Cruz", "T-Shirt")  # la désignation prime sur les réglages du modèle


def test_declared_units_from_invoice_text():
    from converter.analysis import declared_units
    assert declared_units("PAYMENT INFORMATION Total units: 50") == 50
    assert declared_units("Nombre total de produits : 31") == 31
    assert declared_units("Quantité totale livrée : 12") == 12 and declared_units("Total HT : 1395.00 €") is None


def test_sheets_roundtrip_brand_collections_and_title_ignore():
    from converter.storage import doc_to_sheets, sheets_to_doc
    coll = {"allowed": ["A", "B"], "by_type_key": {"tee": ["A", "B"], "cap": "A"}, "by_brand": {"Vans": "B"}}
    sheets = doc_to_sheets("collections.json", coll)
    assert sheets["collections_by_type"][1:] == [["tee", "A"], ["tee", "B"], ["cap", "A"]]
    assert sheets_to_doc("collections.json", sheets) == coll
    rules = {"brands": ["X"], "colors": [], "product_types": [], "sized_numeric_kinds": ["apparel"], "title_ignore": ["(EU)"]}
    assert sheets_to_doc("product_rules.json", doc_to_sheets("product_rules.json", rules)) == rules


def test_board_title_has_color_but_width_is_only_a_variant():
    p = norm("Baker Board Stop And Think Black 8.5")
    assert p.title == "Baker Board - Stop And Think - Black" and (p.option1_name, p.option1_value) == ("Largeur", "8.5")
    a, b = norm("Baker Board Stop And Think Black 8.0"), norm("Baker Board Stop And Think Black 8.5")
    assert a.handle == b.handle  # même planche, deux largeurs = un produit à deux variantes


def test_wheels_title_diameter_then_hardness_uppercase_options_unchanged():
    p = norm("Spitfire Wheels Easy Formula 95A 52mm")
    assert p.title == "Spitfire Wheels - Easy Formula - 52MM - 95A"
    assert (p.option1_name, p.option1_value, p.option2_name, p.option2_value) == ("Dureté", "95A", "Diamètre", "52mm")


def test_eu_shoe_size_in_designation_becomes_variant():
    a, b = norm("Santa Cruz Slides Mens Drift 42 EU"), norm("Santa Cruz Slides Mens Drift 43 EU")
    assert a.option1_name == "Taille" and (a.option1_value, b.option1_value) == ("42", "43")
    assert a.handle == b.handle and "EU" not in a.title and "42" not in a.title


def test_junior_compound_size_recognised():
    rules = {**RULES, "brands": ["Eight Ball"], "product_types": [{"keywords": ["helmet"], "type_key": "helmet", "kind": "other", "label": "Helmet"}]}
    item = LineItem(sku="X", description="Eight Ball Helmet Fit Dial Matte JR/XS Black", qty=1, unit_price_net=10)
    p = normalize_item(item, rules, MAPPING, COLL, 2.4, 20)
    assert p.option1_value == "JR/XS" and p.title == "Eight Ball - Helmet - Fit Dial Matte - Black"


def test_delivery_and_shipping_lines_are_not_products():
    from converter.analysis import drop_non_product_lines
    items = [LineItem(sku=s, description=d, qty=1) for s, d in
             (("A-B-1", "Hoodie Dot"), ("DEL-EUB-0500", "Delivery Charge Non UK Over £425 (500)"), ("SHP", "Shipment"),
              ("C-D-2", "Delivery Bag Black"), ("F", "Frais de port"))]
    kept, dropped = drop_non_product_lines(items)
    assert [i.sku for i in kept] == ["A-B-1", "C-D-2"]  # « Delivery Bag Black » est un vrai produit
    assert "Delivery Charge Non UK Over £425 (500)" in dropped and "Shipment" in dropped and "Frais de port" in dropped


def test_supplier_vat_matching_tolerates_pdf_layout():
    from converter.formats import supplier_in_text
    assert supplier_in_text("GB137 5548 50", "VAT  Reg. No.: GB137  5548  50")  # espaces irréguliers
    assert supplier_in_text("GB137554850", "VAT Reg. No.: GB137 5548 50")
    # TVA française coupée sur deux lignes par la mise en page : retrouvée par son SIREN
    assert supplier_in_text("FR55403419393", "TVA Intracommunautaire : FR 55\n94470 BOISSY  403 419 393")
    assert supplier_in_text("DE160705175", "Willem Kampert No. TVA : DE160705175")
    assert supplier_in_text("adidas France", "ADIDAS FRANCE sarl")
    # pas de faux positifs : le n° de TVA du CLIENT ou un autre fournisseur ne reconnaît pas ce modèle
    assert not supplier_in_text("FR55403419393", "No. TVA FR71419568217 (Wall Street)")
    assert not supplier_in_text("GB137 5548 50", "VAT GB999 1234 56") and not supplier_in_text("", "texte")
