import pandas as pd

from converter.config import load_json, template_columns
from converter.models import LineItem
from converter.normalizer import normalize_item
from converter.preview_edit import dropdown_options, from_editable, split_tags, to_editable
from converter.shopify_builder import build_dataframe

RULES = load_json("product_rules.json")
MAPPING = {**load_json("product_type_mapping.json"), "tee": "T-Shirt"}
COLL = {"allowed": ["Textile", "Skate"], "by_type_key": {}, "by_brand": {}}


def _df():
    items = [
        LineItem(sku="DGK-TEE-0012", description="DGK TEE BLACK", size="M", qty=3, unit_price_net=10.0, public_price=29.9),
        LineItem(sku="DGK-TEE-0012", description="DGK TEE BLACK", size="L", qty=2, unit_price_net=10.0, public_price=29.9),
    ]
    products = [normalize_item(i, RULES, MAPPING, COLL, 2.4, 20) for i in items]
    return build_dataframe(products, template_columns())


def test_round_trip_keeps_csv_identical():
    df = _df()
    back = from_editable(to_editable(df), list(df.columns))
    assert back.to_csv(index=False) == df.to_csv(index=False)


def test_edits_are_written_back_as_csv_text():
    df = _df()
    ed = to_editable(df)
    ed.loc[0, "Vendor"] = "Baker"
    ed.at[0, "Tags"] = ["nouveautés", "Skate"]
    ed.loc[0, "Price"] = 25.5
    ed.loc[1, "Inventory quantity"] = 7
    back = from_editable(ed, list(df.columns))
    assert back.loc[0, "Vendor"] == "Baker"
    assert back.loc[0, "Tags"] == "nouveautés, Skate"
    assert back.loc[0, "Price"] == "25.50"
    assert back.loc[1, "Inventory quantity"] == 7
    assert back.loc[1, "Vendor"] == ""  # cellule vide restée vide


def test_dropdowns_come_from_config_and_keep_current_values():
    df = _df()
    opts = dropdown_options(df, {"brands": ["Baker", "Anti Hero"]}, MAPPING, COLL, {("chaussures", "US"): {"8": "40,5"}})
    assert opts["Vendor"][:2] == ["Baker", "Anti Hero"] and "DGK" in opts["Vendor"]
    assert "T-Shirt" in opts["Type"]
    assert {"Textile", "Skate", "nouveautés"} <= set(opts["Tags"])
    assert "40,5" in opts["Option1 value"] and "M" in opts["Option1 value"]
    assert opts["Status"][0] == "active"


def test_split_tags():
    assert split_tags("a, b,, a") == ["a", "b"]
    assert split_tags(None) == []
