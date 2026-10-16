from converter.config import PROFILES, load_json, template_columns
from converter.normalizer import normalize_item, normalize_size
from converter.pdf_parser import parse_text
from converter.reconciliation import reconcile
from converter.shopify_builder import build_dataframe

RULES = load_json("product_rules.json")
MAPPING = load_json("product_type_mapping.json")
COLL = load_json("collections.json")
BL = PROFILES["BL complet (Qté, PU HT, PU Net, Total, Prix public TTC)"]

SAMPLE = """Page 1
BON DE LIVRAISON
Désignation Qté PU
DGK-TEE-0012 DGK TEE BLACK 2M 3 12,00 10,00 30,00 29,90
3760123456789
DGK-TEE-0013 DGK TEE BLACK 3L
3 12,00 10,00 30,00 29,90
DGK-WAL-0001 DGK Wallet Brown 1 20,00 15,00 15,00 39,90
SPI-WHE-0100 Spitfire Wheels Conical Full Wallstreet 99D 56mm 4 30 25 100 59,90
Total HT 175,00
"""


def test_parse_blocks_and_barcode():
    items = parse_text(SAMPLE, BL)
    assert [i.sku for i in items] == ["DGK-TEE-0012", "DGK-TEE-0013", "DGK-WAL-0001", "SPI-WHE-0100"]
    assert items[0].barcode == "3760123456789"
    assert items[0].qty == 3 and items[0].unit_price_net == 10.0 and items[0].public_price == 29.9
    assert items[1].qty == 3  # numbers on next line
    assert not items[0].warnings


def test_poc_simple_profile():
    items = parse_text("SCA-TEE-12915\nSkate Tee\n2 10.87 21.74\n", PROFILES["Facture simple (Qté, PU HT, Total)"])
    assert items[0].qty == 2 and items[0].total == 21.74


def test_size_normalisation():
    assert normalize_size("2M") == "M" and normalize_size("3L") == "L" and normalize_size("4XL") == "XL"
    assert normalize_size("S/M") == "S/M" and normalize_size("Black") is None


def test_build_csv_grouping_and_rules():
    items = parse_text(SAMPLE, BL)
    prods = [normalize_item(i, RULES, MAPPING, COLL, 2.4, 20) for i in items]
    df = build_dataframe(prods, template_columns())
    assert list(df.columns) == template_columns()
    tee = df[df["URL handle"] == df["URL handle"].iloc[0]]
    assert len(tee) == 2 and tee["Title"].iloc[1] == ""
    assert set(tee["Option1 name"]) == {"Taille"} and list(tee["Option1 value"]) == ["M", "L"]
    assert df["Tags"].iloc[0] == "nouveautés" and df["Published on online store"].iloc[0] == "FALSE"
    wheel = df[df["Option1 name"] == "Dureté"].iloc[0]
    assert wheel["Option2 value"] == "56mm" and wheel["Title"].endswith("99D - 56mm")
    wallet = df[df["Vendor"] == "DGK"].iloc[1]
    assert wallet["Type"] == "Porte Feuille" and wallet["Option1 name"] == ""


def test_reconcile_ok_and_ko():
    po = parse_text("AB-CD-1 x 5 1\nAB-CD-2 y 3 1\n", PROFILES["Bon de commande (Qté, PU)"])
    bl_ok = parse_text("AB-CD-2 y 3 1\nAB-CD-1 x 5 1\n", PROFILES["Bon de commande (Qté, PU)"])
    r = reconcile(po, bl_ok)
    assert r.ok and r.quantities == [5, 3]
    bl_ko = parse_text("AB-CD-1 x 4 1\nAB-CD-9 z 1 1\n", PROFILES["Bon de commande (Qté, PU)"])
    r = reconcile(po, bl_ko)
    assert not r.ok and r.quantities == [4, None] and len(r.discrepancies) == 3


def test_config_validation():
    import pandas as pd
    from converter.config_ui import clean_list, validate_collections, validate_mapping, validate_types

    vals, errs = clean_list(["DGK", " dgk ", "", None, "Enjoi"], "Marques")
    assert vals == ["DGK", "Enjoi"] and len(errs) == 1

    rules, errs = validate_types(pd.DataFrame([
        {"keywords": "Tee, T-Shirt", "type_key": "tee", "kind": "apparel", "label": "T-Shirt"},
        {"keywords": "", "type_key": "tee", "kind": "bad", "label": ""},
    ]))
    assert rules[0]["keywords"] == ["tee", "t-shirt"] and len(errs) == 3

    mapping, errs = validate_mapping(pd.DataFrame([{"type_key": "wallet", "product_type": "Porte Feuille"}]))
    assert mapping == {"wallet": "Porte Feuille"} and not errs

    data, errs = validate_collections(
        pd.DataFrame({"Collection": ["Skate"]}),
        pd.DataFrame([{"type_key": "deck", "collection": "Skate"}, {"type_key": "tee", "collection": "Nope"}]),
    )
    assert data["by_type_key"] == {"deck": "Skate"} and len(errs) == 1


def test_save_json_roundtrip(tmp_path, monkeypatch):
    from converter import config
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    config.save_json("x.json", {"a": "é"})
    assert config.load_json("x.json") == {"a": "é"}
