"""Chargement de la configuration (JSON) et du template Shopify."""
from __future__ import annotations

import io
from pathlib import Path
from typing import Optional

import pandas as pd

from .formats import formats_from_doc, profiles_from_doc
from .storage import DOC_FORMATS, VALID_KINDS, ConfigStore, LocalJsonStore

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DEFAULT_TEMPLATE = BASE_DIR / "data" / "product_template.csv"

# Paramètres fixes issus des règles métier
TAG_NEW = "nouveautés"
WEIGHT_NORMAL_G = 500  # 0.5 kg
WEIGHT_DECK_G = 1000  # 1 kg
WEIGHT_UNIT = "kg"

# Colonnes Shopify de secours si aucun template n'est disponible
FALLBACK_COLUMNS = list(pd.read_csv(DEFAULT_TEMPLATE, nrows=0).columns) if DEFAULT_TEMPLATE.exists() else []


_store: Optional[ConfigStore] = None


def set_store(store: Optional[ConfigStore]) -> None:
    """Définit le backend de configuration actif (None = fichiers JSON locaux)."""
    global _store
    _store = store


def get_store() -> ConfigStore:
    return _store if _store is not None else LocalJsonStore(CONFIG_DIR)


def load_json(name: str) -> dict:
    """Charge un document de config depuis le backend actif (erreurs lisibles)."""
    return get_store().load(name)


def save_json(name: str, data: dict) -> None:
    """Enregistre un document de config dans le backend actif."""
    get_store().save(name, data)


def load_profiles() -> tuple[dict[str, list[str]], list[str]]:
    """Formats fournisseurs enregistrés -> ({nom: colonnes numériques ordonnées}, problèmes de config)."""
    return profiles_from_doc(load_json(DOC_FORMATS))


def load_formats() -> tuple[dict[str, list[str]], dict[str, dict], list[str]]:
    """Tous les formats enregistrés : (fin de ligne {nom: colonnes}, modèles par en-têtes {nom: modèle}, problèmes)."""
    return formats_from_doc(load_json(DOC_FORMATS))


def config_health(rules: dict, mapping: dict, collections: dict, formats: Optional[dict] = None) -> list[str]:
    """Points de configuration à compléter (liste vide = tout est prêt).

    `formats` (document supplier_formats) est optionnel : s'il est fourni, les formats fournisseurs sont contrôlés.
    """
    issues: list[str] = []
    if formats is not None:
        profiles, templates, problems = formats_from_doc(formats)
        if not profiles and not templates:
            issues.append("Aucun format de facture fournisseur valide (la lecture automatique des tableaux reste active)")
        issues.extend(problems)
    if not rules.get("brands"):
        issues.append("Aucune marque définie")
    missing = [r["type_key"] for r in rules.get("product_types", []) if not mapping.get(r["type_key"])]
    if missing:
        issues.append(f"{len(missing)} type(s) sans Product Type : {', '.join(missing)}")
    types = rules.get("product_types", [])
    bad_kind = [r["type_key"] for r in types if r.get("kind") not in VALID_KINDS]
    if bad_kind:
        issues.append(f"kind invalide pour : {', '.join(bad_kind)} (valeurs permises : {', '.join(VALID_KINDS)})")
    keys = [r["type_key"] for r in types]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    if dupes:
        issues.append(f"type_key en double : {', '.join(dupes)}")
    if not collections.get("allowed"):
        issues.append("Liste blanche des collections vide")
    return issues


def template_columns(uploaded: Optional[bytes] = None) -> list[str]:
    """Colonnes du template Shopify (upload > fichier du dépôt). Ordre conservé à l'identique."""
    if uploaded:
        return list(pd.read_csv(io.BytesIO(uploaded), nrows=0).columns)
    if FALLBACK_COLUMNS:
        return FALLBACK_COLUMNS
    raise FileNotFoundError("Aucun template Shopify : ajoutez data/product_template.csv ou importez-le.")
