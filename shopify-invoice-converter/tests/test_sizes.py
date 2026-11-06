"""Conversion des tailles US/UK en tailles françaises."""
from converter.sizes import charts_from_doc, convert_size, marked_system, size_key, validate_charts

DOC = {"charts": [
    {"chart": "Chaussures", "system": "US", "from": "9", "to": "42"},
    {"chart": "Chaussures", "system": "US", "from": "9,5", "to": "42,5"},
    {"chart": "Pantalons", "system": "US", "from": "30", "to": "40"},
    {"chart": "Casquettes", "system": "US", "from": "7 1/8", "to": "57"},
]}
CHARTS = charts_from_doc(DOC)


def test_size_key_normalises_notations():
    assert size_key("US 9,5") == size_key("9.5") == size_key("9 1/2") == "9.5"
    assert size_key("W30") == size_key("30/32") == "30" and size_key("7 1/8") == "7.125" and size_key("m") == "M"


def test_convert_size_by_chart_and_system():
    assert convert_size("9.5", "US", "Chaussures", CHARTS) == ("42,5", None)
    assert convert_size("W30", "US", "Pantalons", CHARTS) == ("40", None)
    assert convert_size("7 1/8", "US", "Casquettes", CHARTS) == ("57", None)
    assert convert_size("42", "", "Chaussures", CHARTS) == ("42", None)  # document déjà en tailles FR
    assert convert_size("M", "US", "Chaussures", CHARTS) == ("M", None)  # lettres : internationales
    assert convert_size("US 9", "", "Chaussures", CHARTS) == ("42", None)  # « US » écrit dans la taille : prime
    assert convert_size("42 EU", "US", "Chaussures", CHARTS) == ("42", None)
    value, problem = convert_size("15", "US", "Chaussures", CHARTS)
    assert value == "15" and "absente du tableau « Chaussures »" in problem
    assert "aucun tableau" in convert_size("9", "US", "", CHARTS)[1]
    assert "pas de lignes UK" in convert_size("9", "UK", "Chaussures", CHARTS)[1]
    assert marked_system("9 US") == "US" and marked_system("42 EU") == "" and marked_system("42") is None


def test_validate_charts():
    assert validate_charts(DOC["charts"]) == []
    errs = validate_charts([{"chart": "A", "system": "US", "from": "9", "to": "42"}, {"chart": "a", "system": "US", "from": "9.0", "to": "43"},
                            {"chart": "B", "system": "JP", "from": "9", "to": "42"}, {"chart": "C", "system": "US", "from": "", "to": "1"}])
    assert len(errs) == 3


def test_normalizer_converts_with_type_chart_or_supplier_default():
    from test_pipeline import COLL, MAPPING, RULES
    from converter.models import LineItem
    from converter.normalizer import normalize_item
    rules = {**RULES, "product_types": [*RULES["product_types"], {"keywords": ["sneaker"], "type_key": "shoes", "kind": "other", "label": "", "size_chart": "Chaussures"}]}
    shoe = LineItem(sku="X-9", description="Vans Sneaker Black", qty=1, unit_price_net=40, size="9.5")
    p = normalize_item(shoe, rules, MAPPING, COLL, 2.4, 20, "US", "", CHARTS)
    assert (p.option1_name, p.option1_value) == ("Taille", "42,5") and not any("Taille" in w for w in p.warnings)
    # type non reconnu (désignation sans « shoe ») : tableau par défaut du modèle fournisseur
    plain = LineItem(sku="C-9", description="Comanche-326 BLACK", qty=1, unit_price_net=45, size="9")
    assert normalize_item(plain, rules, MAPPING, COLL, 2.4, 20, "US", "Chaussures", CHARTS).option1_value == "42"
    assert normalize_item(plain, rules, MAPPING, COLL, 2.4, 20).option1_value == "9"  # pas de système : inchangé
    p = normalize_item(plain, rules, MAPPING, COLL, 2.4, 20, "US", "", CHARTS)
    assert p.option1_value == "9" and any("aucun tableau" in w for w in p.warnings)
    # taille lue dans la désignation d'un pantalon US
    pants = LineItem(sku="P-1", description="Santa Cruz Pants Black 30", qty=1, unit_price_net=20)
    assert normalize_item(pants, RULES, MAPPING, COLL, 2.4, 20, "US", "", CHARTS).option1_value == "40"


def test_shipped_size_config_is_consistent():
    from test_pipeline import COLL, MAPPING, RULES
    from converter.config import config_health, load_json
    sizes = load_json("size_conversions.json")
    assert {"Chaussures", "Pantalons", "Vêtements", "Casquettes"} <= {r["chart"] for r in sizes["charts"]}
    issues = config_health(RULES, MAPPING, COLL, load_json("supplier_formats.json"), sizes)
    assert not any("Tailles" in i or "introuvable" in i for i in issues)
    bad = config_health({**RULES, "product_types": [{"type_key": "x", "kind": "other", "size_chart": "Inconnu"}]}, {"x": "X"}, COLL, None, sizes)
    assert any("introuvable" in i and "Inconnu" in i for i in bad)


def test_sort_section_alphabetical_for_every_config_tab():
    from converter.config_ui import sort_section
    rules = {"brands": ["vans", "Éclair", "adidas"], "colors": ["Red", "black"],
             "product_types": [{"type_key": "tee"}, {"type_key": "cap"}], "sized_numeric_kinds": ["apparel"]}
    assert sort_section("product_rules.json", rules, "brands")["brands"] == ["adidas", "Éclair", "vans"]
    assert sort_section("product_rules.json", rules, "colors")["colors"] == ["black", "Red"]
    assert [t["type_key"] for t in sort_section("product_rules.json", rules, "product_types")["product_types"]] == ["cap", "tee"]
    assert list(sort_section("product_type_mapping.json", {"_comment": "x", "tee": "", "cap": "Cap"})) == ["_comment", "cap", "tee"]
    coll = sort_section("collections.json", {"allowed": ["Textile", "Skate"], "by_type_key": {"tee": "Textile", "deck": "Skate"}})
    assert coll["allowed"] == ["Skate", "Textile"] and list(coll["by_type_key"]) == ["deck", "tee"]
    fmts = sort_section("supplier_formats.json", {"formats": [{"name": "Vans"}, {"name": "adidas"}]})
    assert [f["name"] for f in fmts["formats"]] == ["adidas", "Vans"]
    charts = {"charts": [{"chart": "Chaussures", "system": "US", "from": "10"}, {"chart": "Casquettes", "system": "US", "from": "7"},
                         {"chart": "Chaussures", "system": "US", "from": "9,5"}]}
    assert [(r["chart"], r["from"]) for r in sort_section("size_conversions.json", charts)["charts"]] == [
        ("Casquettes", "7"), ("Chaussures", "9,5"), ("Chaussures", "10")]  # pointures dans l'ordre numérique


def test_sheets_roundtrip_of_size_charts_and_type_chart():
    from test_pipeline import FakeGateway, _local, _strip
    from converter.storage import SheetsStore
    gw = FakeGateway()
    store = SheetsStore(gw, seed=_local())
    assert store.load("size_conversions.json") == _strip(_local().load("size_conversions.json"))
    assert gw.sheets["size_conversions"][0] == ["chart", "system", "from", "to"]
    pants = next(t for t in store.load("product_rules.json")["product_types"] if t["type_key"] == "pants")
    assert pants["size_chart"] == "Pantalons"
    gw.sheets["product_types"] = [["keywords", "type_key", "kind", "label"], ["tee", "tee", "apparel", "T-Shirt"]]  # Sheet ancien
    store.refresh()
    assert store.load("product_rules.json")["product_types"] == [{"keywords": ["tee"], "type_key": "tee", "kind": "apparel", "label": "T-Shirt"}]
