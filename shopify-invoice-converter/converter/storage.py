"""Stockage de la configuration : fichiers JSON locaux OU Google Sheets.

Les trois « documents » de config (product_rules.json, product_type_mapping.json, collections.json)
sont exposés via la même interface `ConfigStore` (load/save/refresh). Avec Google Sheets, chaque
document est éclaté en onglets-tables faciles à éditer à la main (voir SHEET_SCHEMAS).

Ce module n'importe NI Streamlit NI le reste du projet (testable isolément) ; `gspread` n'est
importé que si Google Sheets est réellement configuré.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, Protocol

DOC_RULES = "product_rules.json"
DOC_MAPPING = "product_type_mapping.json"
DOC_COLLECTIONS = "collections.json"

# Onglet -> colonnes attendues (l'ordre dans le Sheet est libre, les noms comptent)
SHEET_SCHEMAS: dict[str, list[str]] = {
    "brands": ["brand"],
    "colors": ["color"],
    "product_types": ["keywords", "type_key", "kind", "label"],
    "settings": ["key", "value"],
    "type_mapping": ["type_key", "product_type"],
    "collections_allowed": ["collection"],
    "collections_by_type": ["type_key", "collection"],
}
DOC_SHEETS: dict[str, list[str]] = {
    DOC_RULES: ["brands", "colors", "product_types", "settings"],
    DOC_MAPPING: ["type_mapping"],
    DOC_COLLECTIONS: ["collections_allowed", "collections_by_type"],
}
ALL_SHEETS = list(SHEET_SCHEMAS)
VALID_KINDS = ["deck", "wheel", "apparel", "cap", "other"]


class StorageError(OSError):
    """Erreur de stockage avec message lisible (sous-classe d'OSError : gérée comme une erreur d'E/S)."""


class ConfigStore(Protocol):
    description: str
    url: Optional[str]

    def load(self, name: str) -> dict: ...
    def save(self, name: str, data: dict) -> None: ...
    def refresh(self) -> None: ...


# ============================ Backend local (JSON) ============================
class LocalJsonStore:
    url: Optional[str] = None

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.description = f"Fichiers JSON locaux ({self.directory.name}/)"

    def load(self, name: str) -> dict:
        path = self.directory / name
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Fichier de configuration introuvable : {path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON invalide dans {path.name} (ligne {exc.lineno}) : {exc.msg}") from exc

    def save(self, name: str, data: dict) -> None:
        """Écriture atomique (fichier temporaire + os.replace)."""
        path = self.directory / name
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, path)
        except OSError as exc:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise StorageError(f"Impossible d'écrire {path.name} : {exc}") from exc

    def refresh(self) -> None:  # rien à rafraîchir : lecture directe du disque
        return None


# ============================ Conversions document <-> onglets ============================
Rows = list[list[str]]


def _norm(rows: Rows) -> Rows:
    """Retire cellules/lignes vides de fin (l'API Google les omet) pour comparer proprement."""
    out: Rows = []
    for r in rows:
        r = [str(c) for c in r]
        while r and r[-1] == "":
            r.pop()
        out.append(r)
    while out and not out[-1]:
        out.pop()
    return out


def _table(rows: Rows, sheet: str) -> list[dict[str, str]]:
    """Lit un onglet en enregistrements ; tolère l'ordre libre des colonnes et colonnes en plus."""
    if not rows:
        return []
    schema = SHEET_SCHEMAS[sheet]
    header = [str(c).strip().lower() for c in rows[0]]
    idx: dict[str, int] = {}
    for col in schema:
        if col not in header:
            raise StorageError(
                f"Onglet « {sheet} » : colonne « {col} » introuvable "
                f"(en-tête attendu : {', '.join(schema)})."
            )
        idx[col] = header.index(col)
    out = []
    for r in rows[1:]:
        rec = {col: (str(r[i]).strip() if i < len(r) else "") for col, i in idx.items()}
        if any(rec.values()):
            out.append(rec)
    return out


def _split(value: str) -> list[str]:
    return [p.strip() for p in value.split(",") if p.strip()]


def sheets_to_doc(name: str, sheets: Mapping[str, Rows]) -> dict:
    """Reconstruit un document de config à partir des onglets."""
    t = lambda s: _table(list(sheets.get(s) or []), s)  # noqa: E731
    if name == DOC_RULES:
        types = [
            {
                "keywords": [k.lower() for k in _split(r["keywords"])],
                "type_key": r["type_key"],
                "kind": r["kind"] or "other",
                "label": r["label"],
            }
            for r in t("product_types")
            if r["type_key"]
        ]
        settings = {r["key"]: r["value"] for r in t("settings")}
        return {
            "brands": [r["brand"] for r in t("brands") if r["brand"]],
            "colors": [r["color"] for r in t("colors") if r["color"]],
            "product_types": types,
            "sized_numeric_kinds": _split(settings.get("sized_numeric_kinds", "apparel")),
        }
    if name == DOC_MAPPING:
        return {r["type_key"]: r["product_type"] for r in t("type_mapping") if r["type_key"]}
    if name == DOC_COLLECTIONS:
        return {
            "allowed": [r["collection"] for r in t("collections_allowed") if r["collection"]],
            "by_type_key": {r["type_key"]: r["collection"] for r in t("collections_by_type") if r["type_key"] and r["collection"]},
        }
    raise StorageError(f"Document de configuration inconnu : {name}")


def doc_to_sheets(name: str, data: Mapping) -> dict[str, Rows]:
    """Éclate un document de config en onglets (en-tête + lignes, tout en texte)."""
    def sheet(name_: str, records: list[list[str]]) -> Rows:
        return [list(SHEET_SCHEMAS[name_]), *records]

    if name == DOC_RULES:
        return {
            "brands": sheet("brands", [[b] for b in data.get("brands", [])]),
            "colors": sheet("colors", [[c] for c in data.get("colors", [])]),
            "product_types": sheet(
                "product_types",
                [[", ".join(r.get("keywords", [])), r.get("type_key", ""), r.get("kind", ""), r.get("label", "")]
                 for r in data.get("product_types", [])],
            ),
            "settings": sheet("settings", [["sized_numeric_kinds", ", ".join(data.get("sized_numeric_kinds", ["apparel"]))]]),
        }
    if name == DOC_MAPPING:
        return {"type_mapping": sheet("type_mapping", [[k, str(v or "")] for k, v in data.items() if not k.startswith("_")])}
    if name == DOC_COLLECTIONS:
        return {
            "collections_allowed": sheet("collections_allowed", [[c] for c in data.get("allowed", [])]),
            "collections_by_type": sheet("collections_by_type", [[k, v] for k, v in data.get("by_type_key", {}).items()]),
        }
    raise StorageError(f"Document de configuration inconnu : {name}")


# ============================ Backend Google Sheets ============================
class SheetsGateway(Protocol):
    description: str
    url: Optional[str]

    def read_all(self, names: list[str]) -> dict[str, Optional[Rows]]: ...  # None = onglet absent
    def write(self, name: str, rows: Rows) -> None: ...  # crée l'onglet si besoin, remplace le contenu


class SheetsStore:
    """Config stockée dans un Google Sheet, avec cache mémoire (TTL) pour économiser le quota d'API.

    - Un seul appel de lecture groupé pour tous les onglets.
    - Onglets absents/vides : créés et pré-remplis depuis `seed` (config locale) à la première connexion.
    - À l'enregistrement, seuls les onglets modifiés sont réécrits.
    - Dernier enregistrement gagnant (pas de verrou multi-utilisateur).
    """

    def __init__(
        self,
        gateway: SheetsGateway,
        seed: Optional[ConfigStore] = None,
        ttl: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._gw, self._seed, self._ttl, self._clock = gateway, seed, ttl, clock
        self._data: Optional[dict[str, Rows]] = None
        self._loaded_at = 0.0
        self._lock = threading.RLock()
        self.description = gateway.description
        self.url = gateway.url

    def _fetch(self) -> dict[str, Rows]:
        data = self._gw.read_all(ALL_SHEETS)
        missing = [s for s in ALL_SHEETS if not data.get(s)]
        if missing:
            self._seed_sheets(missing)
            data = self._gw.read_all(ALL_SHEETS)
        return {s: data.get(s) or [] for s in ALL_SHEETS}

    def _seed_sheets(self, missing: list[str]) -> None:
        for doc, sheets in DOC_SHEETS.items():
            todo = [s for s in sheets if s in missing]
            if not todo:
                continue
            try:
                source = self._seed.load(doc) if self._seed else {}
            except (OSError, ValueError):
                source = {}
            rows = doc_to_sheets(doc, source)
            for s in todo:
                self._gw.write(s, rows[s])

    def _bundle(self) -> dict[str, Rows]:
        with self._lock:
            if self._data is None or self._clock() - self._loaded_at > self._ttl:
                self._data = self._fetch()
                self._loaded_at = self._clock()
            return self._data

    def load(self, name: str) -> dict:
        return sheets_to_doc(name, self._bundle())

    def save(self, name: str, data: dict) -> None:
        new = doc_to_sheets(name, data)
        with self._lock:
            bundle = self._bundle()
            for sheet, rows in new.items():
                if _norm(rows) != _norm(bundle.get(sheet, [])):
                    self._gw.write(sheet, rows)
                    bundle[sheet] = rows

    def refresh(self) -> None:
        with self._lock:
            self._data = None


class GSpreadGateway:
    """Accès réel à Google Sheets via un compte de service (gspread)."""

    def __init__(self, service_account_info: Mapping, spreadsheet: str):
        try:
            import gspread
        except ImportError as exc:
            raise StorageError("Module « gspread » manquant : pip install gspread") from exc
        self._gspread = gspread
        self.email = str(service_account_info.get("client_email", "le compte de service"))
        with self._errors():
            client = gspread.service_account_from_dict(dict(service_account_info))
            ref = spreadsheet.strip()
            self._sh = client.open_by_url(ref) if ref.startswith("http") else client.open_by_key(ref)
        self.description = f"Google Sheet « {self._sh.title} »"
        self.url = self._sh.url

    @contextmanager
    def _errors(self) -> Iterator[None]:
        g = self._gspread
        try:
            yield
        except StorageError:
            raise
        except g.exceptions.SpreadsheetNotFound as exc:
            raise StorageError(f"Google Sheet introuvable. Vérifiez l'ID/l'URL et partagez-le avec {self.email} (rôle Éditeur).") from exc
        except g.exceptions.APIError as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            hints = {
                403: f"Accès refusé : partagez le Sheet avec {self.email} (rôle Éditeur) et activez l'API Google Sheets.",
                404: "Google Sheet introuvable : vérifiez l'ID ou l'URL.",
                429: "Quota Google atteint : patientez une minute puis cliquez sur « Recharger ».",
            }
            raise StorageError(hints.get(code, f"Erreur Google Sheets (code {code}) : {exc}")) from exc
        except (ValueError, KeyError) as exc:
            raise StorageError(f"Identifiants du compte de service invalides : {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - réseau, GSpreadException, etc.
            raise StorageError(f"Connexion à Google Sheets impossible : {exc}") from exc

    def read_all(self, names: list[str]) -> dict[str, Optional[Rows]]:
        with self._errors():
            existing = {ws.title for ws in self._sh.worksheets()}
            present = [n for n in names if n in existing]
            result: dict[str, Optional[Rows]] = {n: None for n in names}
            if present:
                response = self._sh.values_batch_get([f"'{n}'" for n in present])
                for n, value_range in zip(present, response.get("valueRanges", [])):
                    result[n] = value_range.get("values", [])
            return result

    def write(self, name: str, rows: Rows) -> None:
        g = self._gspread
        with self._errors():
            try:
                ws = self._sh.worksheet(name)
                created = False
            except g.exceptions.WorksheetNotFound:
                ws = self._sh.add_worksheet(title=name, rows=max(len(rows) + 50, 100), cols=max(len(rows[0]), 4))
                created = True
            if ws.row_count < len(rows) + 1:
                ws.resize(rows=len(rows) + 50)
            ws.update(values=rows, range_name="A1", raw=True)  # mise à jour d'abord (jamais d'onglet vide)
            if ws.row_count > len(rows):
                ws.batch_clear([f"A{len(rows) + 1}:Z{ws.row_count}"])  # puis purge des lignes en trop
            if created:
                ws.freeze(rows=1)
                ws.format("1:1", {"textFormat": {"bold": True}})
                if name == "product_types":
                    self._kind_dropdown(ws)

    def _kind_dropdown(self, ws) -> None:
        """Liste déroulante sur la colonne « kind » (confort d'édition ; échec sans conséquence)."""
        try:
            self._sh.batch_update({"requests": [{"setDataValidation": {
                "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 1000, "startColumnIndex": 2, "endColumnIndex": 3},
                "rule": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": k} for k in VALID_KINDS]},
                         "showCustomUi": True, "strict": False},
            }}]})
        except Exception:  # noqa: BLE001
            pass


# ============================ Fabrique ============================
def build_store(secrets: Mapping, env: Mapping[str, str], local: ConfigStore) -> ConfigStore:
    """Choisit le backend. Google Sheets si identifiants + Sheet sont fournis, sinon JSON local.

    Sources (dans cet ordre) :
      - secrets Streamlit : [gcp_service_account] (clé JSON du compte de service) et [config_sheet] spreadsheet = "<ID ou URL>"
      - variables d'environnement : GOOGLE_SERVICE_ACCOUNT_JSON (contenu JSON) et CONFIG_SHEET_ID
      - CONFIG_BACKEND=json force le mode local.
    Lève StorageError si Google Sheets est configuré mais inutilisable.
    """
    if env.get("CONFIG_BACKEND", "").strip().lower() == "json":
        return local

    info = secrets.get("gcp_service_account")
    if not info and env.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        try:
            info = json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"])
        except json.JSONDecodeError as exc:
            raise StorageError(f"GOOGLE_SERVICE_ACCOUNT_JSON n'est pas un JSON valide : {exc.msg}") from exc
    sheet = (secrets.get("config_sheet") or {}).get("spreadsheet") or env.get("CONFIG_SHEET_ID")

    if not info and not sheet:
        return local  # Google Sheets non configuré : mode local
    if not info or not sheet:
        raise StorageError("Configuration Google Sheets incomplète : il faut à la fois le compte de service ET l'ID/URL du Sheet.")
    return SheetsStore(GSpreadGateway(info, str(sheet)), seed=local)
