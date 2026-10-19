"""Interface Streamlit de consultation / édition des fichiers de configuration JSON.

Les fonctions `validate_*` sont pures (sans Streamlit) pour pouvoir être testées.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from .config import get_store, load_json, save_json
from .storage import VALID_KINDS

KINDS = VALID_KINDS
KIND_HELP = (
    "deck = largeur (Option « Largeur »), wheel = dureté + diamètre, apparel = taille (lettres et chiffres), "
    "cap = taille S/M…, other = pas de variante sauf taille explicite"
)


# ----------------------------- Validation (pur) -----------------------------
def clean_list(values, label: str) -> tuple[list[str], list[str]]:
    """Retire vides et doublons (insensible à la casse). Retourne (liste, erreurs)."""
    out: list[str] = []
    seen: set[str] = set()
    errors: list[str] = []
    for v in values:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            continue
        v = str(v).strip()
        if not v:
            continue
        if v.lower() in seen:
            errors.append(f"{label} : doublon « {v} »")
            continue
        seen.add(v.lower())
        out.append(v)
    return out, errors


def validate_types(df: pd.DataFrame) -> tuple[list[dict], list[str]]:
    rules: list[dict] = []
    errors: list[str] = []
    keys: set[str] = set()
    for n, (_, r) in enumerate(df.iterrows(), start=1):
        raw = [str(c).strip().lower() for c in str(r.get("keywords") or "").split(",")]
        keywords = [k for k in raw if k]
        type_key = str(r.get("type_key") or "").strip()
        kind = str(r.get("kind") or "").strip()
        label = str(r.get("label") or "").strip()
        if not (keywords or type_key or label):
            continue  # ligne vide
        if not keywords:
            errors.append(f"Ligne {n} : au moins un mot-clé requis")
        if not type_key:
            errors.append(f"Ligne {n} : type_key requis")
        elif type_key in keys:
            errors.append(f"Ligne {n} : type_key « {type_key} » en double")
        keys.add(type_key)
        if kind not in KINDS:
            errors.append(f"Ligne {n} : kind doit être l'un de {KINDS}")
        rules.append({"keywords": keywords, "type_key": type_key, "kind": kind, "label": label})
    return rules, errors


def validate_mapping(df: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    mapping: dict[str, str] = {}
    errors: list[str] = []
    for n, (_, r) in enumerate(df.iterrows(), start=1):
        key = str(r.get("type_key") or "").strip()
        val = r.get("product_type")
        val = "" if val is None or (isinstance(val, float) and pd.isna(val)) else str(val).strip()
        if not key:
            if val:
                errors.append(f"Ligne {n} : type_key manquant")
            continue
        if key in mapping:
            errors.append(f"type_key « {key} » en double")
        mapping[key] = val
    return mapping, errors


def validate_collections(allowed_df: pd.DataFrame, by_df: pd.DataFrame) -> tuple[dict, list[str]]:
    allowed, errors = clean_list(allowed_df["Collection"], "Collections autorisées")
    by_type: dict[str, str] = {}
    for _, r in by_df.iterrows():
        key = str(r.get("type_key") or "").strip()
        coll = str(r.get("collection") or "").strip()
        if not key and not coll:
            continue
        if not key or not coll:
            errors.append("Association type → collection incomplète")
        elif coll not in allowed:
            errors.append(f"« {coll} » n'est pas dans la liste blanche (orthographe exacte requise)")
        elif key in by_type:
            errors.append(f"type_key « {key} » en double")
        else:
            by_type[key] = coll
    return {"allowed": allowed, "by_type_key": by_type}, errors


# ----------------------------- Helpers UI -----------------------------
def _show_errors(errors: list[str]) -> None:
    for e in errors:
        st.error(e)


def _save(name: str, data: dict, comment_from: dict) -> bool:
    """Sauvegarde en conservant la clé _comment ; retourne True si OK."""
    if "_comment" in comment_from:
        data = {"_comment": comment_from["_comment"], **data}
    try:
        save_json(name, data)
    except OSError as exc:
        st.error(str(exc))
        return False
    st.toast(f"{name} enregistré ✅")
    return True


def _list_editor(title: str, file: str, key: str, label: str) -> None:
    data = load_json(file)
    st.caption(f"{len(data[key])} élément(s). Ajoutez une ligne avec ➕ en bas du tableau, supprimez en cochant la ligne puis 🗑️.")
    edited = st.data_editor(
        pd.DataFrame({label: data[key]}), num_rows="dynamic", width="stretch", key=f"ed_{file}_{key}"
    )
    if st.button(f"💾 Enregistrer {title.lower()}", key=f"save_{file}_{key}"):
        values, errors = clean_list(edited[label], title)
        if errors:
            _show_errors(errors)
        elif _save(file, {**data, key: values}, data):
            st.rerun()


def _types_tab() -> None:
    rules = load_json("product_rules.json")
    mapping = load_json("product_type_mapping.json")
    df = pd.DataFrame(
        [{**r, "keywords": ", ".join(r["keywords"])} for r in rules["product_types"]],
        columns=["keywords", "type_key", "kind", "label"],
    )
    st.caption("Mots-clés cherchés dans la désignation. " + KIND_HELP)
    edited = st.data_editor(
        df,
        num_rows="dynamic",
        width="stretch",
        key="ed_types",
        column_config={
            "keywords": st.column_config.TextColumn("Mots-clés (séparés par des virgules)", required=True),
            "type_key": st.column_config.TextColumn("type_key", required=True),
            "kind": st.column_config.SelectboxColumn("kind", options=KINDS, required=True),
            "label": st.column_config.TextColumn("Libellé dans le titre"),
        },
    )
    if st.button("💾 Enregistrer les types", key="save_types"):
        new_rules, errors = validate_types(edited)
        if errors:
            _show_errors(errors)
            return
        # Chaque nouveau type_key reçoit une entrée vide dans le mapping (à compléter ensuite)
        added = [r["type_key"] for r in new_rules if r["type_key"] not in mapping]
        new_mapping = {**mapping, **{k: "" for k in added}}
        if _save("product_rules.json", {**rules, "product_types": new_rules}, rules) and _save(
            "product_type_mapping.json", new_mapping, mapping
        ):
            if added:
                st.toast(f"Mapping à compléter pour : {', '.join(added)}")
            st.rerun()


def _mapping_tab() -> None:
    mapping = load_json("product_type_mapping.json")
    rules = load_json("product_rules.json")
    df = pd.DataFrame(
        [{"type_key": k, "product_type": v} for k, v in mapping.items() if not k.startswith("_")]
    )
    st.caption("Valeur EXACTE du Product Type Shopify (accents et orthographe inclus). Vide = alerte lors de la conversion.")
    edited = st.data_editor(df, num_rows="dynamic", width="stretch", key="ed_mapping")
    unknown = set(edited["type_key"].dropna()) - {r["type_key"] for r in rules["product_types"]}
    if unknown:
        st.warning(f"type_key sans règle de détection (onglet « Types de produit ») : {', '.join(sorted(unknown))}")
    if st.button("💾 Enregistrer le mapping", key="save_mapping"):
        new_mapping, errors = validate_mapping(edited)
        if errors:
            _show_errors(errors)
        elif _save("product_type_mapping.json", new_mapping, mapping):
            st.rerun()


def _collections_tab() -> None:
    data = load_json("collections.json")
    rules = load_json("product_rules.json")
    st.markdown("**Liste blanche** (orthographe exacte)")
    allowed_df = st.data_editor(
        pd.DataFrame({"Collection": data["allowed"]}), num_rows="dynamic", width="stretch", key="ed_allowed"
    )
    st.markdown("**Type détecté → collection** (ajoutée dans `Tags`)")
    by_df = st.data_editor(
        pd.DataFrame(
            [{"type_key": k, "collection": v} for k, v in data["by_type_key"].items()],
            columns=["type_key", "collection"],
        ),
        num_rows="dynamic",
        width="stretch",
        key="ed_by_type",
        column_config={
            "type_key": st.column_config.SelectboxColumn("type_key", options=[r["type_key"] for r in rules["product_types"]]),
        },
    )
    if st.button("💾 Enregistrer les collections", key="save_collections"):
        new_data, errors = validate_collections(allowed_df, by_df)
        if errors:
            _show_errors(errors)
        elif _save("collections.json", new_data, data):
            st.rerun()


@st.dialog("⚙️ Configuration métier", width="large")
def config_dialog() -> None:
    """Fenêtre de consultation/édition des fichiers de config."""
    t_brands, t_colors, t_types, t_map, t_coll, t_raw = st.tabs(
        ["Marques", "Couleurs", "Types de produit", "Mapping Product Type", "Collections", "JSON brut"]
    )
    with t_brands:
        _list_editor("Marques", "product_rules.json", "brands", "Marque")
    with t_colors:
        _list_editor("Couleurs", "product_rules.json", "colors", "Couleur")
    with t_types:
        _types_tab()
    with t_map:
        _mapping_tab()
    with t_coll:
        _collections_tab()
    with t_raw:
        name = st.selectbox("Fichier", ["product_rules.json", "product_type_mapping.json", "collections.json"])
        text = json.dumps(load_json(name), ensure_ascii=False, indent=2)
        st.code(text, language="json")
        st.download_button("📥 Télécharger", text.encode("utf-8"), file_name=name, mime="application/json")
    store = get_store()
    if store.url:
        st.info(
            f"Les modifications sont enregistrées dans **{store.description}** : partagées avec toute l'équipe. "
            "Les personnes qui modifient le Sheet directement sont prises en compte sous ~30 s (ou via « Recharger »)."
        )
        st.link_button("📄 Ouvrir le Google Sheet", store.url)
    else:
        st.info(
            "Les modifications sont écrites dans le dossier `config/`. En local, pensez à les committer sur GitHub ; "
            "sur un hébergement éphémère (ex. Streamlit Cloud) elles seraient perdues au redémarrage : "
            "connectez Google Sheets (voir README) ou téléchargez le JSON et committez-le."
        )
