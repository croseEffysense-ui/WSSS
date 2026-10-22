"""Stockage de la configuration : fichiers JSON locaux OU Google Sheets.

Les trois « documents » de config (product_rules.json, product_type_mapping.json, collections.json)
sont exposés via la même interface `ConfigStore` (load/save/refresh). Avec Google Sheets, chaque
document est éclaté en onglets-tables faciles à éditer à la main (voir SHEET_SCHEMAS).

Ce module n'importe NI Streamlit NI le reste du projet (testable isolément) ; `gspread` n'est
importé que si Google Sheets est réellement configuré.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Optional, Protocol

log = logging.getLogger("converter.storage")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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
                data = json.load(fh)
                return data
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
            log.info("Config locale enregistrée : %s", path)
        except OSError as exc:
            if os.path.exists(tmp):
                os.remove(tmp)
            log.error("Écriture impossible de %s : %s", path, exc)
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
        log.info("Lecture de la config depuis Google Sheets (cache vide ou expiré)…")
        data = self._gw.read_all(ALL_SHEETS)
        missing = [s for s in ALL_SHEETS if not data.get(s)]
        if missing:
            log.warning("Onglets absents ou vides : %s → création et pré-remplissage depuis la config locale", missing)
            self._seed_sheets(missing)
            data = self._gw.read_all(ALL_SHEETS)
        log.info("Config chargée : %s", {s: max(len(data.get(s) or []) - 1, 0) for s in ALL_SHEETS})
        return {s: data.get(s) or [] for s in ALL_SHEETS}

    def _seed_sheets(self, missing: list[str]) -> None:
        for doc, sheets in DOC_SHEETS.items():
            todo = [s for s in sheets if s in missing]
            if not todo:
                continue
            try:
                source = self._seed.load(doc) if self._seed else {}
            except (OSError, ValueError) as exc:
                log.warning("Graine locale illisible pour %s (%s) : onglets créés vides", doc, exc)
                source = {}
            rows = doc_to_sheets(doc, source)
            for s in todo:
                log.info("Initialisation de l'onglet « %s » (%d ligne(s))", s, len(rows[s]) - 1)
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
                    log.info("Enregistrement de l'onglet « %s » (%d ligne(s))", sheet, len(rows) - 1)
                    self._gw.write(sheet, rows)
                    bundle[sheet] = rows
                else:
                    log.debug("Onglet « %s » inchangé : aucune écriture", sheet)

    def refresh(self) -> None:
        log.info("Cache de configuration vidé (rechargement demandé)")
        with self._lock:
            self._data = None


# ============================ Aides au diagnostic (pures) ============================
@dataclass
class ResolvedSettings:
    """Ce que build_store a trouvé comme paramètres, et d'où ça vient."""

    forced_local: bool = False
    info: Optional[dict] = None
    info_source: str = "absent"
    sheet: Optional[str] = None
    sheet_source: str = "absent"
    problems: list[str] = field(default_factory=list)


def resolve_settings(secrets: Mapping, env: Mapping[str, str]) -> ResolvedSettings:
    r = ResolvedSettings()
    if env.get("CONFIG_BACKEND", "").strip().lower() == "json":
        r.forced_local = True
        return r
    if secrets.get("gcp_service_account"):
        r.info, r.info_source = dict(secrets["gcp_service_account"]), "secrets Streamlit [gcp_service_account]"
    elif env.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        try:
            r.info, r.info_source = json.loads(env["GOOGLE_SERVICE_ACCOUNT_JSON"]), "variable GOOGLE_SERVICE_ACCOUNT_JSON"
        except json.JSONDecodeError as exc:
            r.problems.append(f"GOOGLE_SERVICE_ACCOUNT_JSON n'est pas un JSON valide (ligne {exc.lineno}, col {exc.colno}) : {exc.msg}")
    cfg = secrets.get("config_sheet") or {}
    if cfg.get("spreadsheet"):
        r.sheet, r.sheet_source = str(cfg["spreadsheet"]), "secrets Streamlit [config_sheet] spreadsheet"
    elif env.get("CONFIG_SHEET_ID"):
        r.sheet, r.sheet_source = env["CONFIG_SHEET_ID"], "variable CONFIG_SHEET_ID"
    elif "config_sheet" in secrets and not cfg.get("spreadsheet"):
        r.problems.append("La section [config_sheet] existe mais la clé « spreadsheet » est absente ou vide.")
    return r


def extract_spreadsheet_id(ref: str) -> str:
    """Accepte une URL complète ou un ID brut ; lève StorageError si inexploitable."""
    ref = ref.strip().strip("\"'")
    if ref.startswith("http"):
        m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", ref)
        if not m:
            raise StorageError(
                "L'URL du Sheet n'a pas la forme https://docs.google.com/spreadsheets/d/<ID>/… "
                "(copiez l'URL depuis la barre d'adresse du Sheet ouvert)."
            )
        return m.group(1)
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,}", ref):
        raise StorageError(f"« {ref[:40]} » ne ressemble ni à une URL de Google Sheet ni à un ID (44 caractères environ).")
    return ref


def normalize_service_account(info: Mapping) -> tuple[dict, list[str]]:
    """Corrige les défauts classiques de la clé collée dans les secrets ; retourne (info, notes)."""
    fixed = {k: (v.strip() if isinstance(v, str) else v) for k, v in dict(info).items()}
    notes: list[str] = []
    key = fixed.get("private_key")
    if isinstance(key, str) and "\\n" in key and "\n" not in key:
        fixed["private_key"] = key.replace("\\n", "\n")
        notes.append("private_key : séquences « \\n » littérales converties en vrais retours à la ligne (corrigé automatiquement).")
    return fixed, notes


def describe_service_account(info: Mapping) -> dict[str, Any]:
    """Résumé SANS secret du compte de service (sûr à afficher/journaliser)."""
    key = info.get("private_key")
    key_desc: Any = "absente"
    if isinstance(key, str):
        key_desc = {
            "longueur": len(key),
            "commence_par_BEGIN_PRIVATE_KEY": key.strip().startswith("-----BEGIN PRIVATE KEY-----"),
            "finit_par_END_PRIVATE_KEY": key.strip().endswith("-----END PRIVATE KEY-----"),
            "retours_ligne_reels": key.count("\n"),
            "sequences_backslash_n_litterales": key.count("\\n"),
        }
    return {
        "type": info.get("type"),
        "project_id": info.get("project_id"),
        "client_email": info.get("client_email"),
        "private_key_id": (str(info.get("private_key_id"))[:6] + "…") if info.get("private_key_id") else None,
        "token_uri": info.get("token_uri"),
        "private_key": key_desc,
    }


def explain_google_error(exc: BaseException, project_id: str = "?", email: str = "le compte de service") -> str:
    """Traduit une exception Google/réseau en « Type: message → piste de résolution »."""
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None) or getattr(exc, "code", None)
    reasons = ""
    try:
        reasons = json.dumps(response.json().get("error", {}).get("details", []))
    except Exception:  # noqa: BLE001
        pass
    blob = f"{low} {reasons.lower()}"
    short = f"{name}: {text[:300]}"

    if "service_disabled" in blob or "accessnotconfigured" in blob or "has not been used in project" in blob or "is disabled" in blob:
        hint = (f"L'API Google Sheets n'est PAS activée dans le projet « {project_id} ». Activez-la : "
                f"https://console.developers.google.com/apis/api/sheets.googleapis.com/overview?project={project_id} "
                "(attendez ~1 minute après l'activation).")
    elif "failed_precondition" in blob or "not supported for this document" in blob:
        hint = "Ce fichier n'est pas un Google Sheet natif (probablement un .xlsx importé dans Drive). Ouvrez-le puis « Fichier → Enregistrer en tant que Google Sheets »."
    elif code == 403 or "permission" in blob or "caller does not have" in blob:
        hint = f"Accès refusé : partagez le Sheet avec {email} avec le rôle « Éditeur » (bouton Partager du Sheet)."
    elif code == 404 or "not found" in blob:
        hint = "Sheet introuvable : l'ID/URL est faux, ou le Sheet est dans un Drive partagé non accessible au compte de service."
    elif code == 429 or "quota" in blob or "rate limit" in blob:
        hint = "Quota Google atteint (≈60 lectures/min) : patientez une minute puis cliquez sur « Recharger »."
    elif "invalid_grant" in blob and ("signature" in blob or "jwt" in blob):
        hint = "Signature JWT refusée : la clé a été supprimée/révoquée dans Google Cloud, ou private_key est corrompue. Générez une NOUVELLE clé JSON."
    elif "invalid_grant" in blob or "token must be a short-lived" in blob or "invalid jwt" in blob:
        hint = "Jeton refusé : horloge de la machine décalée (synchronisez l'heure) ou clé révoquée."
    elif "invalid_scope" in blob or "unauthorized_client" in blob:
        hint = "Autorisation (scope) refusée pour ce compte de service."
    elif any(k in blob for k in ("pem", "could not deserialize", "unable to load", "asn1", "invalid private key", "no key could be detected")):
        hint = ("private_key illisible : recopiez la valeur EXACTE du champ « private_key » du JSON "
                "(avec -----BEGIN PRIVATE KEY----- … -----END PRIVATE KEY-----). Dans secrets.toml, gardez les « \\n » entre guillemets doubles.")
    elif any(k in name.lower() for k in ("connection", "timeout", "proxy", "transport", "ssl")) or "name or service not known" in blob or "max retries" in blob:
        hint = "Problème réseau : pas d'accès à *.googleapis.com (pare-feu, proxy, VPN, DNS). Testez depuis cette machine : https://sheets.googleapis.com"
    else:
        hint = "Cause non reconnue : consultez le détail technique et le journal (onglet « Journal d'exécution »)."
    return f"{short}  →  {hint}"


# ============================ Accès réel Google Sheets ============================
class GSpreadGateway:
    """Accès réel à Google Sheets via un compte de service (gspread). Journalise chaque étape."""

    def __init__(self, service_account_info: Mapping, spreadsheet: str):
        try:
            import gspread
        except ImportError as exc:
            raise StorageError("Module « gspread » manquant : pip install gspread") from exc
        self._gspread = gspread
        info, notes = normalize_service_account(service_account_info)
        for note in notes:
            log.warning(note)
        self.email = str(info.get("client_email", "le compte de service"))
        self.project_id = str(info.get("project_id", "?"))
        sheet_id = extract_spreadsheet_id(spreadsheet)
        log.info("Connexion à Google Sheets · version gspread %s", getattr(gspread, "__version__", "?"))
        log.info("Compte de service : %s", describe_service_account(info))
        log.info("Sheet cible (ID) : %s", sheet_id)

        with self._step("création des identifiants (lecture de la clé privée)"):
            client = gspread.service_account_from_dict(info, scopes=SCOPES)
        with self._step("ouverture du Sheet (obtention du jeton OAuth puis lecture des métadonnées)"):
            self._sh = client.open_by_key(sheet_id)
        self.description = f"Google Sheet « {self._sh.title} »"
        self.url = self._sh.url
        log.info("Connecté : %s · %s", self.description, self.url)

    @contextmanager
    def _step(self, action: str) -> Iterator[None]:
        """Chronomètre une étape, journalise et convertit toute erreur en StorageError explicite."""
        log.debug("→ %s…", action)
        t0 = time.perf_counter()
        try:
            yield
        except StorageError:
            raise
        except Exception as exc:  # noqa: BLE001 - on enrichit puis on relance proprement
            ms = int((time.perf_counter() - t0) * 1000)
            message = explain_google_error(exc, self.project_id if hasattr(self, "project_id") else "?", self.email if hasattr(self, "email") else "le compte de service")
            log.error("✗ ÉCHEC pendant : %s (%d ms) — %s", action, ms, message, exc_info=True)
            raise StorageError(f"Échec pendant « {action} » — {message}") from exc
        else:
            log.debug("✓ %s (%d ms)", action, int((time.perf_counter() - t0) * 1000))

    def read_all(self, names: list[str]) -> dict[str, Optional[Rows]]:
        with self._step("lecture groupée des onglets"):
            existing = {ws.title for ws in self._sh.worksheets()}
            log.debug("Onglets présents dans le Sheet : %s", sorted(existing))
            present = [n for n in names if n in existing]
            result: dict[str, Optional[Rows]] = {n: None for n in names}
            if present:
                response = self._sh.values_batch_get([f"'{n}'" for n in present])
                for n, value_range in zip(present, response.get("valueRanges", [])):
                    result[n] = value_range.get("values", [])
            return result

    def write(self, name: str, rows: Rows) -> None:
        g = self._gspread
        with self._step(f"écriture de l'onglet « {name} »"):
            try:
                ws = self._sh.worksheet(name)
                created = False
            except g.exceptions.WorksheetNotFound:
                log.info("Création de l'onglet « %s »", name)
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
        except Exception as exc:  # noqa: BLE001
            log.warning("Liste déroulante « kind » non créée (sans conséquence) : %s", exc)


# ============================ Fabrique ============================
def build_store(secrets: Mapping, env: Mapping[str, str], local: ConfigStore) -> ConfigStore:
    """Choisit le backend. Google Sheets si identifiants + Sheet sont fournis, sinon JSON local.

    Sources (dans cet ordre) :
      - secrets Streamlit : [gcp_service_account] (clé JSON du compte de service) et [config_sheet] spreadsheet = "<ID ou URL>"
      - variables d'environnement : GOOGLE_SERVICE_ACCOUNT_JSON (contenu JSON) et CONFIG_SHEET_ID
      - CONFIG_BACKEND=json force le mode local.
    Lève StorageError si Google Sheets est configuré mais inutilisable.
    """
    r = resolve_settings(secrets, env)
    log.info(
        "Choix du backend de config · forcé local=%s · compte de service=%s · sheet=%s",
        r.forced_local, r.info_source, r.sheet_source,
    )
    if r.forced_local:
        log.info("CONFIG_BACKEND=json : fichiers JSON locaux")
        return local
    if r.problems:
        raise StorageError(" ".join(r.problems))
    if not r.info and not r.sheet:
        log.info("Google Sheets non configuré (ni secrets ni variables d'environnement) : mode fichiers JSON locaux")
        return local
    if not r.info or not r.sheet:
        missing = "le compte de service (gcp_service_account / GOOGLE_SERVICE_ACCOUNT_JSON)" if not r.info else "l'ID/URL du Sheet ([config_sheet] spreadsheet / CONFIG_SHEET_ID)"
        raise StorageError(f"Configuration Google Sheets incomplète : il manque {missing}.")
    return SheetsStore(GSpreadGateway(r.info, r.sheet), seed=local)
