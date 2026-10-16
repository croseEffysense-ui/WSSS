"""Chargement de la configuration (JSON) et du template Shopify."""
from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
from typing import Optional

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DEFAULT_TEMPLATE = BASE_DIR / "data" / "product_template.csv"

# Paramètres fixes issus des règles métier
TAG_NEW = "nouveautés"
WEIGHT_NORMAL_G = 500  # 0.5 kg
WEIGHT_DECK_G = 1000  # 1 kg
WEIGHT_UNIT = "kg"

# Profils de colonnes numériques en fin de ligne article (ordre de lecture gauche -> droite)
PROFILES: dict[str, list[str]] = {
    "Facture simple (Qté, PU HT, Total)": ["qty", "unit_price_net", "total"],
    "BL complet (Qté, PU HT, PU Net, Total, Prix public TTC)": [
        "qty", "unit_price_gross", "unit_price_net", "total", "public_price",
    ],
    "BL (Qté, PU HT, PU Net, Prix public TTC)": [
        "qty", "unit_price_gross", "unit_price_net", "public_price",
    ],
    "Bon de commande (Qté, PU)": ["qty", "unit_price_net"],
}

# Colonnes Shopify de secours si aucun template n'est disponible
FALLBACK_COLUMNS = list(pd.read_csv(DEFAULT_TEMPLATE, nrows=0).columns) if DEFAULT_TEMPLATE.exists() else []


def load_json(name: str) -> dict:
    """Charge un fichier JSON de config ; lève une erreur lisible sinon."""
    path = CONFIG_DIR / name
    try:
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Fichier de configuration introuvable : {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON invalide dans {path.name} (ligne {exc.lineno}) : {exc.msg}") from exc


def save_json(name: str, data: dict) -> None:
    """Écrit un fichier de config de façon atomique (pas de fichier tronqué en cas d'erreur)."""
    path = CONFIG_DIR / name
    fd, tmp = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise OSError(f"Impossible d'écrire {path.name} : {exc}") from exc


def config_health(rules: dict, mapping: dict, collections: dict) -> list[str]:
    """Points de configuration à compléter (liste vide = tout est prêt)."""
    issues: list[str] = []
    if not rules.get("brands"):
        issues.append("Aucune marque définie")
    missing = [r["type_key"] for r in rules.get("product_types", []) if not mapping.get(r["type_key"])]
    if missing:
        issues.append(f"{len(missing)} type(s) sans Product Type : {', '.join(missing)}")
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
