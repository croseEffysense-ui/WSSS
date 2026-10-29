from converter.config import load_json, load_profiles, template_columns
from converter.normalizer import normalize_item, normalize_size
from converter.pdf_parser import parse_text
from converter.reconciliation import reconcile
from converter.shopify_builder import build_dataframe

RULES = load_json("product_rules.json")
MAPPING = load_json("product_type_mapping.json")
COLL = load_json("collections.json")
PROFILES, _ = load_profiles()
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


def test_detect_profile_picks_best_format():
    from converter.pdf_parser import detect_profile
    name, items = detect_profile(SAMPLE, PROFILES)
    assert name.startswith("BL complet") and len(items) == 4
    po_text = "AB-CD-1 Tee 5 10,00\nAB-CD-2 Cap 3 12,00\n"
    name, items = detect_profile(po_text, PROFILES)
    assert name.startswith("Bon de commande") and items[0].qty == 5


def test_config_health():
    from converter.config import config_health
    issues = config_health(RULES, MAPPING, COLL)
    assert any("sans Product Type" in i for i in issues)
    assert config_health({"brands": ["A"], "product_types": []}, {}, {"allowed": ["X"]}) == []


def test_reconcile_rows():
    po = parse_text("AB-CD-1 x 5 1\nAB-CD-2 y 3 1\n", PROFILES["Bon de commande (Qté, PU)"])
    bl = parse_text("AB-CD-1 x 4 1\nAB-CD-9 z 1 1\n", PROFILES["Bon de commande (Qté, PU)"])
    statuses = [r["Statut"] for r in reconcile(po, bl).rows]
    assert statuses == ["⚠️ Écart de quantité", "❌ Absent du BL", "➕ Absent du PO"]


def test_title_has_no_duplicated_type_keyword():
    from converter.models import LineItem
    p = normalize_item(LineItem(sku="DGK-WAL-0001", description="DGK Wallet Brown", qty=1, unit_price_net=15, public_price=39.9),
                       RULES, MAPPING, COLL, 2.4, 20)
    assert p.title == "DGK Wallet - Brown"
    p = normalize_item(LineItem(sku="SPI-WHE-0100", description="Spitfire Wheels Conical Full Wallstreet 99D 56mm", qty=4, unit_price_net=25),
                       RULES, MAPPING, COLL, 2.4, 20)
    assert p.title == "Spitfire Wheels - Conical Full Wallstreet - 99D - 56mm"


# ----------------------------- Stockage Google Sheets (passerelle simulée) -----------------------------
class FakeGateway:
    description = "Google Sheet « test »"
    url = "https://example.test/sheet"

    def __init__(self):
        self.sheets: dict = {}
        self.writes: list[str] = []
        self.reads = 0

    def read_all(self, names):
        self.reads += 1
        return {n: self.sheets.get(n) for n in names}

    def write(self, name, rows):
        self.writes.append(name)
        self.sheets[name] = [list(r) for r in rows]


def _local():
    from converter.storage import LocalJsonStore
    from converter.config import CONFIG_DIR
    return LocalJsonStore(CONFIG_DIR)


def _strip(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


def test_sheets_seed_and_roundtrip_equals_local():
    from converter.storage import ALL_SHEETS, SheetsStore
    gw = FakeGateway()
    store = SheetsStore(gw, seed=_local())
    for doc in ("product_rules.json", "product_type_mapping.json", "collections.json", "supplier_formats.json"):
        assert store.load(doc) == _strip(_local().load(doc))
    assert set(gw.sheets) == set(ALL_SHEETS)  # onglets créés et pré-remplis


def test_sheets_save_writes_only_changed_sheets_and_caches():
    from converter.storage import SheetsStore
    gw = FakeGateway()
    store = SheetsStore(gw, seed=_local())
    mapping = store.load("product_type_mapping.json")
    reads_before, gw.writes = gw.reads, []
    store.load("collections.json"); store.load("product_rules.json")
    assert gw.reads == reads_before  # servi par le cache
    mapping["deck"] = "Planche"
    store.save("product_type_mapping.json", mapping)
    assert gw.writes == ["type_mapping"]
    store.save("product_type_mapping.json", mapping)  # inchangé : aucune écriture
    assert gw.writes == ["type_mapping"]
    assert store.load("product_type_mapping.json")["deck"] == "Planche"


def test_sheets_direct_edit_visible_after_refresh_and_column_order_free():
    from converter.storage import SheetsStore
    gw = FakeGateway()
    store = SheetsStore(gw, seed=_local())
    store.load("product_type_mapping.json")
    gw.sheets["type_mapping"] = [["product_type", "type_key", "notes"], ["Casquette", "cap", "x"], ["", "", ""]]
    assert store.load("product_type_mapping.json").get("cap") != "Casquette"  # cache encore valide
    store.refresh()
    assert store.load("product_type_mapping.json") == {"cap": "Casquette"}


def test_sheets_missing_column_gives_clear_error():
    import pytest
    from converter.storage import SheetsStore, StorageError
    gw = FakeGateway()
    store = SheetsStore(gw, seed=_local())
    store.load("collections.json")
    gw.sheets["type_mapping"] = [["type_key"], ["cap"]]
    store.refresh()
    with pytest.raises(StorageError, match="product_type"):
        store.load("product_type_mapping.json")


def test_build_store_selection():
    import pytest
    from converter.storage import StorageError, build_store
    local = _local()
    assert build_store({}, {}, local) is local  # rien de configuré
    assert build_store({"config_sheet": {"spreadsheet": "x"}, "gcp_service_account": {"a": 1}}, {"CONFIG_BACKEND": "json"}, local) is local
    with pytest.raises(StorageError, match="incomplète"):
        build_store({"config_sheet": {"spreadsheet": "x"}}, {}, local)
    with pytest.raises(StorageError, match="JSON"):
        build_store({}, {"GOOGLE_SERVICE_ACCOUNT_JSON": "{bad", "CONFIG_SHEET_ID": "x"}, local)


def test_config_health_flags_invalid_kind_and_duplicates():
    from converter.config import config_health
    rules = {"brands": ["A"], "product_types": [
        {"type_key": "a", "kind": "oops"}, {"type_key": "a", "kind": "deck"}]}
    out = " ".join(config_health(rules, {"a": "X"}, {"allowed": ["Y"]}))
    assert "kind invalide" in out and "en double" in out


# ----------------------------- Logs & diagnostic -----------------------------
def test_redact_masks_secrets_but_keeps_plain_hints():
    from converter.logs import redact
    pem = "-----BEGIN PRIVATE KEY-----\n" + "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC" * 4 + "\n-----END PRIVATE KEY-----\n"
    assert "MIIEvQ" not in redact(f"clé: {pem}") and "[CLÉ PRIVÉE MASQUÉE]" in redact(pem)
    assert "MIIEvQ" not in redact('{"private_key": "-----BEGIN PRIVATE KEY-----\\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASCMIIEvQIBADANBgkq\\n"}')
    assert "ya29" not in redact("token ya29.a0AfH6SMBxyz_123-abc") and "eyJ" not in redact("jwt eyJhbGciOi.eyJpc3MiOiJ4.c2lnbmF0dXJl")
    assert "-----BEGIN PRIVATE KEY----- … -----END" in redact("avec -----BEGIN PRIVATE KEY----- … -----END PRIVATE KEY-----")


def test_log_buffer_is_redacted_and_idempotent_setup():
    import logging
    from converter.logs import BUFFER, get_entries, setup_logging
    setup_logging("ERROR"); setup_logging("ERROR")
    logger = logging.getLogger("converter.test")
    assert len([h for h in logging.getLogger("converter").handlers if h is BUFFER]) == 1
    logger.info("Authorization: Bearer ya29.SECRETSECRETSECRETSECRET1234")
    assert not any("SECRETSECRET" in e.message for e in get_entries()) and any("converter.test" == e.logger for e in get_entries())


def test_explain_google_error_cases():
    from gspread.exceptions import APIError
    from converter.storage import explain_google_error

    class R:
        def __init__(self, code, status, message, reason=""):
            self.status_code, self.text = code, message
            self._p = {"error": {"code": code, "message": message, "status": status, "details": [{"reason": reason}]}}
        def json(self): return self._p

    disabled = explain_google_error(APIError(R(403, "PERMISSION_DENIED", "Google Sheets API has not been used in project 42 before or it is disabled.", "SERVICE_DISABLED")), "42", "bot@x")
    assert "API Google Sheets n'est PAS activée" in disabled and "project=42" in disabled
    assert "Éditeur" in explain_google_error(APIError(R(403, "PERMISSION_DENIED", "The caller does not have permission")), "p", "bot@x")
    assert "introuvable" in explain_google_error(APIError(R(404, "NOT_FOUND", "Requested entity was not found.")))
    assert ".xlsx" in explain_google_error(APIError(R(400, "FAILED_PRECONDITION", "This operation is not supported for this document")))
    assert "réseau" in explain_google_error(ConnectionError("Max retries exceeded"))


def _offline(monkeypatch):
    import requests
    def boom(*a, **k): raise requests.exceptions.ConnectionError("Name or service not known")
    monkeypatch.setattr(requests, "get", boom)


def test_diagnostics_nothing_configured_skips_everything_else(monkeypatch):
    from converter.diagnostics import run_diagnostics
    _offline(monkeypatch)
    steps = {s.name: s for s in run_diagnostics({}, {})}
    assert steps["Choix du backend"].status == "error"
    assert steps["Compte de service : champs"].status == "skip" and steps["Réseau vers Google"].status == "skip"


def test_diagnostics_first_blocker_is_reported_and_chain_stops(monkeypatch):
    from converter.diagnostics import run_diagnostics
    _offline(monkeypatch)
    secrets = {"gcp_service_account": {"type": "service_account", "project_id": "p", "private_key": "pas-une-cle",
                                       "client_email": "bot@p.iam.gserviceaccount.com", "token_uri": "https://x"},
               "config_sheet": {"spreadsheet": "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit"}}
    steps = run_diagnostics(secrets, {})
    by = {s.name: s for s in steps}
    assert [s.name for s in steps if s.status == "error"][0] == "Compte de service : champs"
    assert by["Référence du Sheet"].status == "skip" and by["Accès au Sheet (partage + API activée)"].status == "skip"
    assert by["Réseau vers Google"].status == "error"  # indépendant de la clé : toujours testé
    assert not any("KeyError" in s.detail for s in steps)
    assert "BEGIN PRIVATE KEY" in by["Compte de service : champs"].detail  # texte d'aide non masqué


def test_diagnostics_toml_error_is_surfaced(monkeypatch):
    from converter.diagnostics import run_diagnostics
    _offline(monkeypatch)
    s = {x.name: x for x in run_diagnostics({}, {}, secrets_error="TomlDecodeError: Invalid value (at line 4)")}
    assert s["Lecture des secrets Streamlit"].status == "error" and "line 4" in s["Lecture des secrets Streamlit"].detail


def test_broken_secrets_toml_is_detected_not_silently_ignored(tmp_path, monkeypatch):
    from converter.diagnostics import inspect_secrets_files, run_diagnostics, secrets_toml_problem
    _offline(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".streamlit").mkdir()
    (tmp_path / ".streamlit" / "secrets.toml").write_text('[config_sheet]\nspreadsheet = "https://docs.google.com/x\n', encoding="utf-8")
    found = [f for f in inspect_secrets_files() if f.path.parent.parent == tmp_path]
    assert found and found[0].exists and found[0].toml_error and "line" in found[0].toml_error.lower()
    assert secrets_toml_problem() and str(tmp_path) in secrets_toml_problem()
    steps = {s.name: s for s in run_diagnostics({}, {})}
    assert steps["Fichiers secrets.toml"].status == "error" and "INVALIDE" in steps["Fichiers secrets.toml"].detail


def test_misnamed_secrets_file_is_visible(tmp_path, monkeypatch):
    from converter.diagnostics import inspect_secrets_files
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".streamlit").mkdir()
    (tmp_path / ".streamlit" / "secrets.toml.txt").write_text("a = 1", encoding="utf-8")
    f = [x for x in inspect_secrets_files() if x.path.parent.parent == tmp_path][0]
    assert not f.exists and "secrets.toml.txt" in f.siblings


# ----------------------------- Formats fournisseurs -----------------------------
def test_shipped_formats_are_all_valid():
    from converter.config import config_health
    profiles, problems = load_profiles()
    assert len(profiles) == 4 and not problems
    assert not any("format" in i.lower() for i in config_health(RULES, MAPPING, COLL, _local().load("supplier_formats.json")))


def test_field_key_accepts_labels_keys_and_aliases():
    from converter.formats import columns_from_text, columns_to_text, field_key
    assert field_key("Qté") == "qty" and field_key("PU HT (brut)") == "unit_price_gross" and field_key("pu") == "unit_price_net"
    assert field_key("Prix public TTC") == "public_price" and field_key("total_ht") == "total" and field_key("n'importe quoi") is None
    cols = columns_from_text("Qté, PU Net, Total")
    assert cols == ["qty", "unit_price_net", "total"] and columns_from_text(columns_to_text(cols)) == cols
    assert columns_from_text("qty, bidule") == ["qty", "bidule"]  # inconnu conservé pour être signalé


def test_validate_format_rules():
    from converter.formats import validate_format
    assert validate_format("DGK", ["qty", "total"]) == []
    assert any("indispensable" in e for e in validate_format("X", ["total"]))
    assert any("double" in e for e in validate_format("X", ["qty", "qty"]))
    assert any("inconnue" in e for e in validate_format("X", ["qty", "bidule"]))
    assert any("requis" in e for e in validate_format("", ["qty"])) and any("au moins" in e for e in validate_format("X", []))


def test_profiles_from_doc_skips_invalid_and_duplicates_with_report():
    from converter.formats import profiles_from_doc
    doc = {"formats": [
        {"name": "A", "columns": ["qty", "total"]},
        {"name": "a", "columns": ["qty"]},  # doublon (casse ignorée)
        {"name": "B", "columns": ["total"]},  # sans Qté
        {"name": "", "columns": ["qty"]},  # sans nom
    ]}
    profiles, problems = profiles_from_doc(doc)
    assert profiles == {"A": ["qty", "total"]} and len(problems) == 3


def test_new_supplier_format_is_used_for_parsing():
    from converter.formats import profiles_from_doc
    profiles, _ = profiles_from_doc({"formats": [{"name": "Fournisseur X", "columns": ["qty", "public_price", "unit_price_net"]}]})
    item = parse_text("AB-CD-1 Tee 2 39,90 15,00\n", profiles["Fournisseur X"])[0]
    assert item.qty == 2 and item.public_price == 39.9 and item.unit_price_net == 15.0


def test_validate_formats_table():
    import pandas as pd
    from converter.config_ui import FORMAT_COLUMNS, validate_formats

    def row(name, *cols, notes=""):
        return {"name": name, **{c: (cols[i] if i < len(cols) else None) for i, c in enumerate(FORMAT_COLUMNS)}, "notes": notes}

    ok, errs = validate_formats(pd.DataFrame([row("DGK", "Qté", None, "PU Net", "Total", notes="2025"), row(None)]))
    assert not errs and ok == [{"name": "DGK", "columns": ["qty", "unit_price_net", "total"], "notes": "2025"}]  # trou compacté, ligne vide ignorée

    _, errs = validate_formats(pd.DataFrame([row("A", "Qté"), row("a", "Total"), row("", "Qté", "Qté")]))
    assert len(errs) == 4  # doublon de nom, Qté manquante, nom requis, colonne en double

    formats, errs = validate_formats(pd.DataFrame([row(None)]))
    assert formats == [] and not errs  # aucun format fin de ligne : permis (modèles et lecture auto des tableaux)


def test_sheets_formats_roundtrip_and_hand_edit():
    from converter.storage import SheetsStore
    gw = FakeGateway()
    store = SheetsStore(gw, seed=_local())
    doc = store.load("supplier_formats.json")
    doc["formats"].append({"name": "Spitfire", "columns": ["qty", "unit_price_net"], "notes": "BL 2025"})
    store.save("supplier_formats.json", doc)
    assert gw.writes[-1] == "supplier_formats" and gw.sheets["supplier_formats"][-1] == ["Spitfire", "Qté, PU Net", "BL 2025"]
    # édition à la main dans le Sheet : libellés libres, ordre des colonnes libre
    gw.sheets["supplier_formats"] = [["notes", "columns", "name"], ["", "quantité, pu ht, total", "Vans"]]
    store.refresh()
    assert store.load("supplier_formats.json") == {"formats": [{"name": "Vans", "columns": ["qty", "unit_price_gross", "total"], "notes": ""}]}


def test_local_save_formats_roundtrip(tmp_path):
    from converter.storage import LocalJsonStore
    store = LocalJsonStore(tmp_path)
    store.save("supplier_formats.json", {"formats": [{"name": "É", "columns": ["qty"], "notes": ""}]})
    assert store.load("supplier_formats.json")["formats"][0]["name"] == "É"
