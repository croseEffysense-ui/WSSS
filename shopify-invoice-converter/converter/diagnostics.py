"""Diagnostic pas à pas de la connexion Google Sheets (pur : sans Streamlit).

Chaque étape retourne un statut (ok / warn / error / skip / info), un détail et, en cas
d'échec, une piste de résolution. Les étapes dépendantes sont ignorées après un échec, pour
désigner clairement LE premier blocage. Aucun secret n'est affiché.
"""
from __future__ import annotations

import logging
import os
import platform
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .logs import entries_text, redact
from .storage import (
    ALL_SHEETS,
    DOC_COLLECTIONS,
    DOC_MAPPING,
    DOC_RULES,
    SCOPES,
    SHEET_SCHEMAS,
    ConfigStore,
    StorageError,
    _table,
    build_store,
    describe_service_account,
    explain_google_error,
    extract_spreadsheet_id,
    normalize_service_account,
    resolve_settings,
)

log = logging.getLogger("converter.diagnostics")
ICONS = {"ok": "✅", "warn": "⚠️", "error": "❌", "skip": "⏭️", "info": "ℹ️"}
TEST_SHEET = "_diagnostic_tmp"


@dataclass
class Step:
    name: str
    status: str
    detail: str = ""
    hint: str = ""
    duration_ms: int = 0
    trace: str = ""


Result = tuple[str, str, str]  # (statut, détail, piste)


@dataclass
class SecretsFile:
    path: Path
    exists: bool
    size: int = 0
    toml_error: Optional[str] = None
    siblings: tuple[str, ...] = ()  # autres fichiers du dossier .streamlit (détecte secrets.toml.txt, secret.toml…)


def _toml_error(path: Path) -> Optional[str]:
    """Message d'erreur TOML précis (ligne/colonne) ou None si le fichier est valide."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"fichier illisible ({type(exc).__name__}: {exc}) : enregistrez-le en UTF-8"
    try:
        try:
            import tomllib as loader
        except ImportError:  # Python < 3.11
            import toml as loader  # type: ignore[no-redef]
        loader.loads(text)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def inspect_secrets_files() -> list[SecretsFile]:
    """Emplacements où Streamlit cherche secrets.toml, avec validité TOML de ceux qui existent."""
    base = Path(__file__).resolve().parent.parent
    candidates = dict.fromkeys([Path.cwd() / ".streamlit" / "secrets.toml", base / ".streamlit" / "secrets.toml", Path.home() / ".streamlit" / "secrets.toml"])
    out = []
    for path in candidates:
        siblings = tuple(sorted(p.name for p in path.parent.iterdir())) if path.parent.is_dir() else ()
        if path.is_file():
            out.append(SecretsFile(path, True, path.stat().st_size, _toml_error(path), siblings))
        else:
            out.append(SecretsFile(path, False, 0, None, siblings))
    return out


def secrets_toml_problem() -> Optional[str]:
    """Si un secrets.toml EXISTE mais est invalide, retourne l'erreur. (Streamlit la déguise en « fichier absent ».)"""
    for f in inspect_secrets_files():
        if f.exists and f.toml_error:
            return f"{f.path} — {f.toml_error}"
    return None


def _pkg_version(name: str) -> str:
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:  # noqa: BLE001
        return "non installé"


def run_diagnostics(
    secrets: Mapping,
    env: Mapping[str, str],
    secrets_error: Optional[str] = None,
    write_test: bool = False,
    local_store: Optional[ConfigStore] = None,
) -> list[Step]:
    steps: list[Step] = []
    ctx: dict[str, Any] = {}
    first_error: list[str] = []

    def add(name: str, fn: Callable[[], Result], needs: str = "") -> None:
        """needs="chain" : ignorée dès qu'une étape précédente a échoué ;
        needs="config" : ignorée seulement si Google Sheets n'est pas configuré."""
        if (needs == "chain" and first_error) or (needs == "config" and not ctx.get("configured")):
            reason = first_error[0] if first_error else "Choix du backend"
            steps.append(Step(name, "skip", f"Ignoré : l'étape « {reason} » a échoué."))
            return
        t0 = time.perf_counter()
        trace = ""
        try:
            status, detail, hint = fn()
        except Exception as exc:  # noqa: BLE001 - le diagnostic ne doit jamais planter
            trace = traceback.format_exc()
            status = "error"
            # Une StorageError porte déjà un message complet (étape + cause + piste) : on ne l'enrobe pas deux fois.
            detail = str(exc) if isinstance(exc, StorageError) else explain_google_error(
                exc, ctx.get("project_id", "?"), ctx.get("email", "le compte de service"))
            hint = ""
        ms = int((time.perf_counter() - t0) * 1000)
        if status == "error" and not first_error:
            first_error.append(name)
        steps.append(Step(name, status, redact(detail), redact(hint), ms, redact(trace)))
        level = {"error": logging.ERROR, "warn": logging.WARNING}.get(status, logging.INFO)
        log.log(level, "[diagnostic] %s → %s · %s", name, status, detail)

    log.info("===== Début du diagnostic Google Sheets =====")

    # 1. Environnement ---------------------------------------------------------------
    def env_step() -> Result:
        return "info", (
            f"Python {sys.version.split()[0]} · {platform.system()} · gspread {_pkg_version('gspread')} · "
            f"google-auth {_pkg_version('google-auth')} · streamlit {_pkg_version('streamlit')} · "
            f"requests {_pkg_version('requests')}\nDossier courant : {Path.cwd()}\n"
            f"Heure locale UTC : {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}"
        ), ""
    add("Environnement", env_step)

    # 2. Fichiers secrets.toml ---------------------------------------------------------
    def files_step() -> Result:
        files = inspect_secrets_files()
        lines = []
        for f in files:
            if f.exists:
                state = f"✔ trouvé ({f.size} octets) · TOML " + ("valide" if not f.toml_error else f"INVALIDE → {f.toml_error}")
            else:
                state = "✘ absent"
            lines.append(f"{state}  {f.path}")
            if f.siblings and not f.exists:
                lines.append(f"     contenu du dossier : {list(f.siblings)}")
        broken = [f for f in files if f.exists and f.toml_error]
        if broken:
            return "error", "\n".join(lines), (
                "Le fichier existe mais n'est pas du TOML valide : Streamlit l'ignore ALORS EN SILENCE (comme s'il était absent). "
                "Corrigez la ligne indiquée (cause fréquente : guillemet fermant oublié, ou clé privée collée sur plusieurs lignes sans triple guillemets).")
        if not any(f.exists for f in files):
            return "warn", "\n".join(lines), (
                "Aucun secrets.toml trouvé. Copiez .streamlit/secrets.toml.example vers .streamlit/secrets.toml (nom EXACT ; attention à "
                "« secrets.toml.txt » sous Windows) dans le dossier d'où vous lancez « streamlit run ». Sur Streamlit Cloud : Settings → Secrets.")
        return "ok", "\n".join(lines), ""
    add("Fichiers secrets.toml", files_step)

    # 3. Lecture des secrets Streamlit --------------------------------------------------
    def secrets_step() -> Result:
        if secrets_error:
            return "error", f"Les secrets n'ont pas pu être lus : {secrets_error}", (
                "Le fichier secrets.toml est probablement mal formé (TOML invalide : guillemets, crochets, "
                "chaînes multi-lignes). Comparez-le avec .streamlit/secrets.toml.example.")
        keys = {k: (sorted(v.keys()) if isinstance(v, Mapping) else type(v).__name__) for k, v in secrets.items()}
        if not keys:
            return "warn", "Aucune section trouvée parmi [gcp_service_account] / [config_sheet].", (
                "Vérifiez les noms de sections (exactement gcp_service_account et config_sheet).")
        return "ok", "Sections lues : " + "; ".join(f"[{k}] → {v}" for k, v in keys.items()), ""
    add("Lecture des secrets Streamlit", secrets_step)

    # 4. Variables d'environnement ------------------------------------------------------
    def env_vars_step() -> Result:
        names = ["CONFIG_BACKEND", "CONFIG_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON", "LOG_LEVEL"]
        present = [n for n in names if env.get(n)]
        proxies = [n for n in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY") if os.environ.get(n)]
        detail = f"Définies : {present or 'aucune'} · Proxy détecté : {proxies or 'aucun'}"
        return "info", detail, ""
    add("Variables d'environnement", env_vars_step)

    # 5. Choix du backend ---------------------------------------------------------------
    settings = resolve_settings(secrets, env)
    ctx["settings"] = settings

    def backend_step() -> Result:
        if settings.forced_local:
            return "info", "CONFIG_BACKEND=json : mode local forcé, Google Sheets volontairement ignoré.", "Supprimez CONFIG_BACKEND pour utiliser Google Sheets."
        if settings.problems:
            return "error", " ".join(settings.problems), ""
        if not settings.info and not settings.sheet:
            return "error", "Google Sheets n'est pas configuré : ni compte de service, ni ID de Sheet trouvés (secrets ou variables).", (
                "Renseignez [gcp_service_account] et [config_sheet] dans secrets.toml (voir étapes précédentes).")
        if not settings.info:
            return "error", f"Sheet trouvé ({settings.sheet_source}) mais AUCUN compte de service.", "Ajoutez la section [gcp_service_account] avec la clé JSON."
        if not settings.sheet:
            return "error", f"Compte de service trouvé ({settings.info_source}) mais AUCUN Sheet.", "Ajoutez [config_sheet] spreadsheet = \"<URL ou ID>\"."
        return "ok", f"Compte de service : {settings.info_source} · Sheet : {settings.sheet_source}", ""
    add("Choix du backend", backend_step)
    if settings.forced_local:
        first_error.append("Choix du backend")
    ctx["configured"] = bool(settings.info and settings.sheet and not settings.problems and not settings.forced_local)

    # 6. Champs du compte de service ----------------------------------------------------
    def fields_step() -> Result:
        info, notes = normalize_service_account(settings.info or {})
        ctx.update(info=info, project_id=str(info.get("project_id", "?")), email=str(info.get("client_email", "le compte de service")))
        required = ["type", "project_id", "private_key", "client_email", "token_uri"]
        missing = [k for k in required if not info.get(k)]
        desc = describe_service_account(info)
        detail = f"client_email = {desc['client_email']} · project_id = {desc['project_id']} · type = {desc['type']}\nclé privée : {desc['private_key']}"
        if notes:
            detail += "\nCorrections automatiques : " + " ".join(notes)
        if missing:
            return "error", f"Champs manquants dans [gcp_service_account] : {missing}\n{detail}", "Recopiez TOUS les champs du fichier JSON téléchargé depuis Google Cloud."
        problems = []
        if info.get("type") != "service_account":
            problems.append(f"type = « {info.get('type')} » (attendu : service_account : vous avez peut-être utilisé un identifiant OAuth « client » au lieu d'un compte de service)")
        if "@" not in str(info.get("client_email")) or ".gserviceaccount.com" not in str(info.get("client_email")):
            problems.append("client_email ne ressemble pas à un compte de service (…@….iam.gserviceaccount.com)")
        key = str(info.get("private_key"))
        if not key.strip().startswith("-----BEGIN PRIVATE KEY-----"):
            problems.append("private_key ne commence pas par -----BEGIN PRIVATE KEY-----")
        if "\n" not in key:
            problems.append("private_key ne contient aucun retour à la ligne")
        if problems:
            return "error", detail + "\n" + "\n".join("• " + p for p in problems), "Corrigez les champs ci-dessus."
        return "ok", detail, ""
    add("Compte de service : champs", fields_step, needs="chain")

    # 7. Référence du Sheet -------------------------------------------------------------
    def sheet_ref_step() -> Result:
        sheet_id = extract_spreadsheet_id(settings.sheet or "")
        ctx["sheet_id"] = sheet_id
        kind = "URL" if (settings.sheet or "").strip().startswith("http") else "ID brut"
        return "ok", f"{kind} reconnu · ID du Sheet = {sheet_id} ({len(sheet_id)} caractères)", ""
    add("Référence du Sheet", sheet_ref_step, needs="chain")

    # 8. Clé privée lisible -------------------------------------------------------------
    def creds_step() -> Result:
        from google.oauth2 import service_account
        ctx["creds"] = service_account.Credentials.from_service_account_info(ctx["info"], scopes=SCOPES)
        return "ok", "La clé privée est valide et lisible (signature JWT possible).", ""
    add("Clé privée lisible", creds_step, needs="chain")

    # 9. Réseau + horloge -----------------------------------------------------------------
    def network_step() -> Result:
        import requests
        t0 = time.perf_counter()
        r = requests.get("https://sheets.googleapis.com/", timeout=10)
        ms = int((time.perf_counter() - t0) * 1000)
        ctx["http_date"] = r.headers.get("Date")
        return "ok", f"sheets.googleapis.com joignable (HTTP {r.status_code}, {ms} ms)", ""
    add("Réseau vers Google", network_step, needs="config")

    def clock_step() -> Result:
        date_header = ctx.get("http_date")
        if not date_header:
            return "skip", "En-tête Date indisponible.", ""
        skew = (datetime.now(timezone.utc) - parsedate_to_datetime(date_header)).total_seconds()
        if abs(skew) > 60:
            return "warn", f"Horloge décalée de {skew:+.0f} s par rapport à Google.", "Synchronisez l'heure du serveur/PC : un décalage > quelques minutes fait refuser le jeton (invalid_grant)."
        return "ok", f"Décalage d'horloge : {skew:+.1f} s (acceptable)", ""
    add("Horloge système", clock_step, needs="config")

    # 10. Jeton OAuth -------------------------------------------------------------------
    def token_step() -> Result:
        from google.auth.transport.requests import Request
        creds = ctx["creds"]
        creds.refresh(Request())
        expiry = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None
        left = int((expiry - datetime.now(timezone.utc)).total_seconds()) if expiry else "?"
        return "ok", f"Jeton OAuth obtenu (valable ~{left} s). La clé est acceptée par Google.", ""
    add("Authentification Google (jeton)", token_step, needs="chain")

    # 11. Accès au Sheet -----------------------------------------------------------------
    def open_step() -> Result:
        import gspread
        client = gspread.authorize(ctx["creds"])
        sh = client.open_by_key(ctx["sheet_id"])
        ctx["sh"] = sh
        titles = [ws.title for ws in sh.worksheets()]
        ctx["titles"] = titles
        return "ok", f"Sheet ouvert : « {sh.title} » · {sh.url}\nOnglets existants : {titles}", ""
    add("Accès au Sheet (partage + API activée)", open_step, needs="chain")

    # 12. Onglets attendus ---------------------------------------------------------------
    def tabs_step() -> Result:
        existing = set(ctx["titles"])
        missing = [s for s in ALL_SHEETS if s not in existing]
        ctx["missing"] = missing
        if missing:
            return "warn", f"Onglets manquants : {missing}", ("Ils seront créés et pré-remplis automatiquement au premier chargement "
                                                              "(nécessite le rôle Éditeur). Vous pouvez aussi les créer à la main (voir README).")
        return "ok", f"Les {len(ALL_SHEETS)} onglets attendus existent.", ""
    add("Onglets attendus", tabs_step, needs="chain")

    # 13. Lecture et validation des en-têtes ------------------------------------------------
    def read_step() -> Result:
        present = [s for s in ALL_SHEETS if s in ctx["titles"]]
        if not present:
            return "info", "Aucun onglet à lire pour l'instant.", ""
        response = ctx["sh"].values_batch_get([f"'{n}'" for n in present])
        lines, errors = [], []
        for name, vr in zip(present, response.get("valueRanges", [])):
            rows = vr.get("values", [])
            try:
                n = len(_table(rows, name))
                lines.append(f"✔ {name} : {n} ligne(s) de données · en-tête {rows[0] if rows else '(vide)'}")
            except StorageError as exc:
                errors.append(f"✘ {exc}")
        if errors:
            return "error", "\n".join(lines + errors), "Corrigez l'en-tête de l'onglet (colonnes exactes : " + "; ".join(f"{k}={v}" for k, v in SHEET_SCHEMAS.items()) + ")."
        return "ok", "\n".join(lines), ""
    add("Lecture et en-têtes des onglets", read_step, needs="chain")

    # 14. Droit d'écriture (optionnel) ------------------------------------------------------
    def write_step() -> Result:
        sh = ctx["sh"]
        ws = sh.add_worksheet(title=TEST_SHEET, rows=2, cols=2)
        try:
            ws.update(values=[["ok"]], range_name="A1")
        finally:
            sh.del_worksheet(ws)
        return "ok", f"Création, écriture puis suppression de l'onglet « {TEST_SHEET} » réussies : droit Éditeur confirmé.", ""
    if write_test:
        add("Droit d'écriture (rôle Éditeur)", write_step, needs="chain")
    else:
        steps.append(Step("Droit d'écriture (rôle Éditeur)", "skip", "Non demandé (cochez l'option pour le tester)."))

    # 15. Chargement complet comme l'app -----------------------------------------------------
    def full_step() -> Result:
        missing = ctx.get("missing", [])
        if missing and not write_test:
            return "skip", "Des onglets manquent : le chargement complet les créerait (écritures).", "Cochez le test d'écriture pour l'exécuter."
        from .config import CONFIG_DIR
        from .storage import LocalJsonStore
        store = build_store(secrets, env, local_store or LocalJsonStore(CONFIG_DIR))
        rules, mapping, coll = store.load(DOC_RULES), store.load(DOC_MAPPING), store.load(DOC_COLLECTIONS)
        return "ok", (
            f"Chargement complet réussi : {len(rules['brands'])} marque(s), {len(rules['colors'])} couleur(s), "
            f"{len(rules['product_types'])} type(s), {len(mapping)} mapping(s), {len(coll['allowed'])} collection(s)."
        ), ""
    add("Chargement complet (comme l'application)", full_step, needs="chain")

    n_err = sum(1 for s in steps if s.status == "error")
    log.info("===== Fin du diagnostic : %d erreur(s) · premier blocage : %s =====", n_err, first_error[0] if first_error else "aucun")
    return steps


def report_text(steps: list[Step], log_lines: int = 150) -> str:
    """Rapport texte complet (secrets masqués) à transmettre pour obtenir de l'aide."""
    out = [f"RAPPORT DE DIAGNOSTIC — {datetime.now():%Y-%m-%d %H:%M:%S}", "=" * 60]
    for s in steps:
        out.append(f"{ICONS[s.status]} {s.name} [{s.status}] ({s.duration_ms} ms)")
        if s.detail:
            out.append("   " + s.detail.replace("\n", "\n   "))
        if s.hint:
            out.append("   💡 " + s.hint)
        if s.trace:
            out.append("   --- trace ---\n   " + s.trace.strip().replace("\n", "\n   "))
    out += ["", "=" * 60, f"DERNIERS LOGS ({log_lines} lignes max)", "-" * 60]
    out += entries_text("DEBUG").splitlines()[-log_lines:]
    return redact("\n".join(out))
