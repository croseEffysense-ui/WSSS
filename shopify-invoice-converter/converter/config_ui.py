"""Interface Streamlit de consultation / édition des fichiers de configuration JSON.

Les fonctions `validate_*` sont pures (sans Streamlit) pour pouvoir être testées.
"""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from .config import get_store, load_json, save_json
from .formats import (
    FIELD_LABELS, MAX_COLUMNS, TABLE_FIELDS, field_key, normalize_label, profiles_from_doc, validate_format, validate_table,
)
from .sizes import DOC_SIZES, SIZE_SYSTEMS, chart_names, size_key, validate_charts
from .storage import DOC_FORMATS, VALID_KINDS

KINDS = VALID_KINDS
# Libellés affichés pour le « kind » (le code avant « · » est ce qui est enregistré)
KIND_LABELS = {
    "deck": "deck · Largeur (8.25…)",
    "wheel": "wheel · Dureté + Diamètre (99D, 56mm)",
    "apparel": "apparel · Taille (S, M… ou 28, 30…)",
    "cap": "cap · Taille (S/M, L/XL…)",
    "other": "other · Aucune (sauf taille donnée par la facture)",
}
KIND_HELP = (
    "deck = variante Largeur, wheel = Dureté + Diamètre, apparel = Taille en lettres ou en chiffres, "
    "cap = Taille en lettres, other = pas de variante sauf si la facture donne la taille"
)


def kind_code(value) -> str:
    """« apparel · Taille (…) » ou « apparel » -> « apparel »."""
    return str(value or "").split("·")[0].strip()


# ----------------------------- Validation (pur) -----------------------------
def _blank(value) -> bool:
    """Cellule vide : None, NaN ou pd.NA (les tableaux sont en texte, une cellule vide vaut pd.NA)."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):  # liste, dict… : jamais « vide » au sens d'une cellule
        return False


def _cell(value) -> str:
    return "" if _blank(value) else str(value).strip()


def _text_df(df: pd.DataFrame) -> pd.DataFrame:
    """Toutes les colonnes en texte : un tableau vide serait sinon vu comme numérique (et refuserait le texte)."""
    return df.astype("string")


def clean_list(values, label: str) -> tuple[list[str], list[str]]:
    """Retire vides et doublons (insensible à la casse). Retourne (liste, erreurs)."""
    out: list[str] = []
    seen: set[str] = set()
    errors: list[str] = []
    for v in values:
        if _blank(v):
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
        raw = [str(c).strip().lower() for c in _cell(r.get("keywords")).split(",")]
        keywords = [k for k in raw if k]
        type_key = _cell(r.get("type_key"))
        kind = kind_code(_cell(r.get("kind")))
        label = _cell(r.get("label"))
        size_chart = _cell(r.get("size_chart"))
        if not (keywords or type_key or label):
            continue  # ligne vide
        if not keywords:
            errors.append(f"Ligne {n} : au moins un mot qui identifie le type est requis")
        if not type_key:
            errors.append(f"Ligne {n} : code du type requis")
        elif type_key in keys:
            errors.append(f"Ligne {n} : code du type « {type_key} » en double")
        keys.add(type_key)
        if kind not in KINDS:
            errors.append(f"Ligne {n} : choisissez les variantes créées parmi {KINDS}")
        rules.append({"keywords": keywords, "type_key": type_key, "kind": kind, "label": label,
                      **({"size_chart": size_chart} if size_chart else {})})
    return rules, errors


def validate_size_charts(df: pd.DataFrame) -> tuple[list[dict], list[str]]:
    """Tableau éditable (chart, system, from, to) -> (lignes à enregistrer, erreurs)."""
    rows = [
        {"chart": _cell(r.get("chart")), "system": _cell(r.get("system")).upper(), "from": _cell(r.get("from")), "to": _cell(r.get("to"))}
        for _, r in df.iterrows()
    ]
    rows = [r for r in rows if any(r.values())]
    return rows, validate_charts(rows)


def validate_mapping(df: pd.DataFrame) -> tuple[dict[str, str], list[str]]:
    mapping: dict[str, str] = {}
    errors: list[str] = []
    for n, (_, r) in enumerate(df.iterrows(), start=1):
        key = _cell(r.get("type_key"))
        val = _cell(r.get("product_type"))
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
        key = _cell(r.get("type_key"))
        coll = _cell(r.get("collection"))
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


FORMAT_COLUMNS = [f"Colonne {i}" for i in range(1, MAX_COLUMNS + 1)]


def validate_formats(df: pd.DataFrame) -> tuple[list[dict], list[str]]:
    """Tableau éditable (name, Colonne 1..N, notes) -> (formats à enregistrer, erreurs)."""
    formats: list[dict] = []
    errors: list[str] = []
    names: set[str] = set()
    for n, (_, r) in enumerate(df.iterrows(), start=1):
        name, notes = _cell(r.get("name")), _cell(r.get("notes"))
        labels = [_cell(r.get(c)) for c in FORMAT_COLUMNS]
        columns = [field_key(lb) or lb.lower() for lb in labels if lb]  # cases vides ignorées : l'ordre reste celui de lecture
        if not (name or columns or notes):
            continue  # ligne vide
        errors += [f"Ligne {n} : {e}" for e in validate_format(name, columns)]
        if name:
            if name.lower() in names:
                errors.append(f"Ligne {n} : fournisseur « {name} » en double")
            names.add(name.lower())
        formats.append({"name": name, "columns": columns, "notes": notes})
    return formats, errors


def validate_templates(df: pd.DataFrame) -> tuple[list[dict], list[str]]:
    """Tableau éditable (name, match, un en-tête par champ, notes) -> (modèles par en-têtes à enregistrer, erreurs)."""
    templates: list[dict] = []
    errors: list[str] = []
    names: set[str] = set()
    for n, (_, r) in enumerate(df.iterrows(), start=1):
        name, match, notes = _cell(r.get("name")), _cell(r.get("match")), _cell(r.get("notes"))
        system, chart = _cell(r.get("size_system")).upper(), _cell(r.get("size_chart"))
        table = {fld: _cell(r.get(label)) for fld, label in TABLE_FIELDS.items() if _cell(r.get(label))}
        if not (name or table or match or notes):
            continue  # ligne vide
        errors += [f"Modèle {n} : {e}" for e in validate_table(name, table)]
        if system and system not in SIZE_SYSTEMS:
            errors.append(f"Modèle {n} : système de tailles « {system} » inconnu (vide = FR/EU, ou {', '.join(SIZE_SYSTEMS)})")
        if name:
            if name.lower() in names:
                errors.append(f"Modèle {n} : fournisseur « {name} » en double")
            names.add(name.lower())
        entry = {"name": name, "table": table, "match": match}
        entry.update({k: v for k, v in (("size_system", system), ("size_chart", chart)) if v})
        templates.append({**entry, "notes": notes})
    return templates, errors


def _alpha(text) -> str:
    """Clé de tri alphabétique : sans accents ni casse (« Éclair » se range avec « eclair »)."""
    return normalize_label(text)


def sort_section(doc_name: str, data: dict, section: str = "") -> dict:
    """Copie de `data` avec les éléments de la section triés par ordre alphabétique."""
    if doc_name == "product_rules.json" and section in ("brands", "colors"):
        return {**data, section: sorted(data.get(section, []), key=_alpha)}
    if doc_name == "product_rules.json" and section == "product_types":
        return {**data, "product_types": sorted(data.get("product_types", []), key=lambda r: _alpha(r.get("type_key", "")))}
    if doc_name == "product_type_mapping.json":
        meta = {k: v for k, v in data.items() if k.startswith("_")}
        return {**meta, **dict(sorted(((k, v) for k, v in data.items() if not k.startswith("_")), key=lambda kv: _alpha(kv[0])))}
    if doc_name == "collections.json":
        return {
            **data,
            "allowed": sorted(data.get("allowed", []), key=_alpha),
            "by_type_key": dict(sorted(data.get("by_type_key", {}).items(), key=lambda kv: _alpha(kv[0]))),
        }
    if doc_name == DOC_FORMATS:
        return {**data, "formats": sorted(data.get("formats", []), key=lambda f: _alpha(f.get("name", "")))}
    if doc_name == DOC_SIZES:
        def size_order(r: dict) -> tuple:
            key = size_key(r.get("from", ""))
            try:
                number = float(key)
            except ValueError:
                number = float("inf")
            return _alpha(r.get("chart", "")), str(r.get("system", "")), number, key
        return {**data, "charts": sorted(data.get("charts", []), key=size_order)}
    raise ValueError(f"Tri non prévu pour {doc_name} / {section}")


# ----------------------------- Helpers UI -----------------------------
def _ver() -> int:
    """Version des éditeurs : changée après un tri pour que les tableaux se réaffichent dans le nouvel ordre."""
    return st.session_state.get("cfg_editor_ver", 0)


def _sort_button(doc_name: str, data: dict, section: str = "", help: str = "") -> None:
    """Bouton « Trier A → Z » : trie la section et l'enregistre aussitôt."""
    if st.button(
        "🔤 Trier A → Z", key=f"sort_{doc_name}_{section}",
        help=(help + " " if help else "") + "Trie par ordre alphabétique et enregistre aussitôt "
        "(enregistrez d'abord vos modifications en cours, elles seraient perdues).",
    ):
        if _save(doc_name, sort_section(doc_name, data, section), data):
            st.session_state["cfg_editor_ver"] = _ver() + 1
            st.rerun(scope="fragment")


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


def _list_editor(title: str, file: str, key: str, label: str, intro: str = "", column_title: str = "") -> None:
    data = load_json(file)
    if intro:
        st.caption(intro)
    st.caption(f"{len(data[key])} élément(s). Ajoutez une ligne avec ➕ en bas du tableau, supprimez en cochant la ligne puis 🗑️.")
    _sort_button(file, data, key)
    edited = st.data_editor(
        _text_df(pd.DataFrame({label: data[key]})), num_rows="dynamic", width="stretch", key=f"ed_{file}_{key}_{_ver()}",
        column_config={label: st.column_config.TextColumn(column_title or label, width="large")},
    )
    if st.button(f"💾 Enregistrer {title.lower()}", key=f"save_{file}_{key}"):
        values, errors = clean_list(edited[label], title)
        if errors:
            _show_errors(errors)
        elif _save(file, {**data, key: values}, data):
            st.rerun(scope="fragment")


def _types_tab() -> None:
    rules = load_json("product_rules.json")
    mapping = load_json("product_type_mapping.json")
    df = _text_df(pd.DataFrame(
        [{**r, "keywords": ", ".join(r["keywords"]), "kind": KIND_LABELS.get(r.get("kind"), r.get("kind"))}
         for r in rules["product_types"]],
        columns=["keywords", "type_key", "kind", "label", "size_chart"],
    ))
    st.caption(
        "Une ligne par famille de produits. Si un des **mots** est trouvé dans la désignation, le produit est de ce "
        "type. **Les lignes sont testées de haut en bas : la première qui correspond gagne.** Le **code** relie ce "
        "type aux onglets Tailles, Type Shopify et Collections."
    )
    _sort_button(
        "product_rules.json", rules, "product_types",
        help="Tri par type_key. ⚠️ L'ordre compte pour la détection : le premier type dont un mot-clé est trouvé "
        "dans la désignation l'emporte (ex. « hoodie » doit passer avant « tee » pour « Hood Tee »).",
    )
    edited = st.data_editor(
        df,
        num_rows="dynamic",
        width="stretch",
        key=f"ed_types_{_ver()}",
        column_config={
            "keywords": st.column_config.TextColumn(
                "Mots qui identifient le type", required=True, width="large",
                help="Séparés par des virgules, ex. « tee, t-shirt, tshirt ». Cherchés en mots entiers, sans tenir compte des majuscules.",
            ),
            "type_key": st.column_config.TextColumn(
                "Code du type", required=True, help="Identifiant court sans espace (ex. tee). Invisible dans Shopify.",
            ),
            "kind": st.column_config.SelectboxColumn(
                "Variantes créées", options=list(KIND_LABELS.values()), required=True, width="medium", help=KIND_HELP,
            ),
            "label": st.column_config.TextColumn("Mot dans le titre", help="Écrit après la marque dans le titre (ex. T-Shirt)."),
            "size_chart": st.column_config.SelectboxColumn(
                "Tableau de tailles US → FR", options=chart_names(load_json(DOC_SIZES)),
                help="Utilisé seulement si la facture est en tailles US/UK.",
            ),
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
            st.rerun(scope="fragment")


def _mapping_tab() -> None:
    mapping = load_json("product_type_mapping.json")
    rules = load_json("product_rules.json")
    df = _text_df(pd.DataFrame(
        [{"type_key": k, "product_type": v} for k, v in mapping.items() if not k.startswith("_")],
        columns=["type_key", "product_type"],
    ))
    st.caption(
        "Pour chaque type de produit, la catégorie **Product Type** à écrire dans la colonne *Type* du CSV, "
        "**orthographe exacte** de Shopify (accents compris). Vide = colonne Type vide + alerte."
    )
    _sort_button("product_type_mapping.json", mapping, help="Tri par code du type.")
    edited = st.data_editor(
        df, num_rows="dynamic", width="stretch", key=f"ed_mapping_{_ver()}",
        column_config={
            "type_key": st.column_config.TextColumn("Code du type", help="Le même code que dans l'onglet Types de produit."),
            "product_type": st.column_config.TextColumn(
                "Product Type Shopify (exact)", width="large", help="Ex. « Porte Feuille », « T-Shirt », « Chaussures »…",
            ),
        },
    )
    unknown = set(edited["type_key"].dropna()) - {r["type_key"] for r in rules["product_types"]}
    if unknown:
        st.warning(f"Code(s) absent(s) de l'onglet « Types de produit » : {', '.join(sorted(unknown))}")
    if st.button("💾 Enregistrer le mapping", key="save_mapping"):
        new_mapping, errors = validate_mapping(edited)
        if errors:
            _show_errors(errors)
        elif _save("product_type_mapping.json", new_mapping, mapping):
            st.rerun(scope="fragment")


def _collections_tab() -> None:
    data = load_json("collections.json")
    rules = load_json("product_rules.json")
    _sort_button("collections.json", data, help="Trie la liste blanche et les associations (par type_key).")
    st.markdown("**1. Vos collections Shopify** (liste blanche : seules celles-ci peuvent être ajoutées en tag)")
    allowed_df = st.data_editor(
        _text_df(pd.DataFrame({"Collection": data["allowed"]})), num_rows="dynamic", width="stretch", key=f"ed_allowed_{_ver()}",
        column_config={"Collection": st.column_config.TextColumn("Nom exact de la collection Shopify", width="large")},
    )
    st.markdown("**2. Quelle collection pour quel type de produit** (son nom est ajouté dans la colonne *Tags*)")
    by_df = st.data_editor(
        _text_df(pd.DataFrame(
            [{"type_key": k, "collection": v} for k, v in data["by_type_key"].items()],
            columns=["type_key", "collection"],
        )),
        num_rows="dynamic",
        width="stretch",
        key=f"ed_by_type_{_ver()}",
        column_config={
            "type_key": st.column_config.SelectboxColumn("Code du type", options=[r["type_key"] for r in rules["product_types"]]),
            "collection": st.column_config.SelectboxColumn(
                "Collection ajoutée en tag", options=data["allowed"], help="À choisir dans la liste 1 (enregistrez-la d'abord).",
            ),
        },
    )
    if st.button("💾 Enregistrer les collections", key="save_collections"):
        new_data, errors = validate_collections(allowed_df, by_df)
        if errors:
            _show_errors(errors)
        elif _save("collections.json", new_data, data):
            st.rerun(scope="fragment")


def _is_template(entry: dict) -> bool:
    return bool(entry.get("table"))


def _templates_editor(doc: dict) -> None:
    st.markdown("**Modèles par en-têtes de colonnes** (recommandé : fonctionne quel que soit le nombre de colonnes)")
    st.caption(
        "Pour chaque information, recopiez l'en-tête de la colonne tel qu'il est écrit sur la facture "
        "(ex. Qté → « Quantité Totale », PU Net → « Prix Net »). Le plus simple : déposez une facture puis "
        "utilisez « ➕ Créer un modèle fournisseur » affiché sous le PDF. **Identifiant** = texte présent sur les factures "
        "de ce fournisseur (nom, n° de TVA) pour que la détection automatique le choisisse. **Tailles** = système "
        "des tailles de ses factures (vide = déjà FR/EU) ; **Tableau tailles** = correspondances à utiliser quand le "
        "type de produit n'est pas reconnu (ex. fournisseur de chaussures)."
    )
    rows = [
        {"name": f.get("name", ""), "match": f.get("match", ""),
         **{label: f["table"].get(fld) for fld, label in TABLE_FIELDS.items()},
         "size_system": f.get("size_system", ""), "size_chart": f.get("size_chart", ""), "notes": f.get("notes", "")}
        for f in doc.get("formats", []) if _is_template(f)
    ]
    edited = st.data_editor(
        _text_df(pd.DataFrame(rows, columns=["name", "match", *TABLE_FIELDS.values(), "size_system", "size_chart", "notes"])),
        num_rows="dynamic",
        width="stretch",
        key=f"ed_templates_{_ver()}",
        column_config={
            "name": st.column_config.TextColumn("Fournisseur", required=True),
            "match": st.column_config.TextColumn(
                "Texte qui le reconnaît", help="Présent sur toutes ses factures : nom de la société, n° de TVA… (pas le vôtre !)",
            ),
            **{lab: st.column_config.TextColumn(
                f"Colonne « {lab} »", help=f"En-tête exact, sur la facture, de la colonne qui contient : {lab}"
                + (" (plusieurs colonnes possibles : Produit + Couleur)" if fld == "description" else ""),
            ) for fld, lab in TABLE_FIELDS.items()},
            "size_system": st.column_config.SelectboxColumn("Ses tailles sont en", options=SIZE_SYSTEMS, help="Vide = tailles déjà FR/EU"),
            "size_chart": st.column_config.SelectboxColumn(
                "Tableau de tailles par défaut", options=chart_names(load_json(DOC_SIZES)),
                help="Utilisé quand le type de produit n'est pas reconnu (ex. fournisseur de chaussures).",
            ),
            "notes": st.column_config.TextColumn("Notes"),
        },
    )
    if st.button("💾 Enregistrer les modèles", key="save_templates"):
        templates, errors = validate_templates(edited)
        names = {t["name"].lower() for t in templates}
        errors += [f"« {f['name']} » existe déjà dans les formats fin de ligne" for f in doc.get("formats", [])
                   if not _is_template(f) and str(f.get("name", "")).lower() in names]
        if errors:
            _show_errors(errors)
        elif _save(DOC_FORMATS, {**doc, "formats": [*(f for f in doc.get("formats", []) if not _is_template(f)), *templates]}, doc):
            st.rerun(scope="fragment")


def _sizes_tab() -> None:
    doc = load_json(DOC_SIZES)
    st.caption(
        "Correspondances taille étrangère → taille française. Un **tableau** (ex. « Chaussures ») s'associe à un type "
        "de produit (onglet « Types de produit ») ou à un modèle fournisseur. Les tailles en lettres (S, M, L…) ne sont "
        "jamais converties. Les valeurs fournies sont des barèmes courants : vérifiez-les pour vos marques."
    )
    for e in validate_charts(doc.get("charts", [])):
        st.warning(e)
    names = chart_names(doc)
    pick = st.selectbox("Afficher le tableau", ["Tous", *names], key="sizes_filter")
    _sort_button(DOC_SIZES, doc, help="Tri par tableau, puis par taille d'origine (dans l'ordre des pointures : 9 avant 10).")
    rows = [r for r in doc.get("charts", []) if pick == "Tous" or r.get("chart") == pick]
    edited = st.data_editor(
        _text_df(pd.DataFrame(rows, columns=["chart", "system", "from", "to"])),
        num_rows="dynamic",
        width="stretch",
        key=f"ed_sizes_{pick}_{_ver()}",
        column_config={
            "chart": st.column_config.TextColumn("Nom du tableau", required=True, help="Nouveau nom = nouveau tableau (ex. « Chaussures DVS »)"),
            "system": st.column_config.SelectboxColumn("Tailles de la facture en", options=SIZE_SYSTEMS, required=True),
            "from": st.column_config.TextColumn("Taille écrite sur la facture", required=True, help="ex. 9, 9.5, 9 1/2, W30, 7 1/8"),
            "to": st.column_config.TextColumn("Taille française", required=True, help="Ce qui sera écrit dans Shopify, ex. 42,5"),
        },
    )
    if st.button("💾 Enregistrer les tailles", key="save_sizes"):
        new_rows, errors = validate_size_charts(edited)
        kept = [r for r in doc.get("charts", []) if pick != "Tous" and r.get("chart") != pick]  # autres tableaux inchangés
        errors = errors or validate_charts([*kept, *new_rows])
        if errors:
            _show_errors(errors)
        elif _save(DOC_SIZES, {**doc, "charts": [*kept, *new_rows]}, doc):
            st.rerun(scope="fragment")


def _formats_tab() -> None:
    doc = load_json(DOC_FORMATS)
    _, problems = profiles_from_doc(doc)
    for p in problems:
        st.warning(p)
    _sort_button(DOC_FORMATS, doc, help="Trie les modèles de facture et les formats « fin de ligne » par nom de fournisseur.")
    _templates_editor(doc)
    st.divider()
    st.markdown("**Formats « colonnes de fin de ligne »** (références de la forme AAA-BBB-1234)")
    st.caption(
        "**Colonne 1 → 5** = colonnes numériques à la fin d'une ligne article, "
        "**de gauche à droite** comme sur la facture (laissez vide les cases inutilisées). "
        "Le format est ensuite proposé dans la liste « Format fournisseur » lors de l'analyse d'un PDF."
    )
    rows = []
    for f in (f for f in doc.get("formats", []) if not _is_template(f)):
        cols = [FIELD_LABELS.get(c) for c in f.get("columns", [])]
        row = {"name": f.get("name", "")}
        row.update({c: (cols[i] if i < len(cols) else None) for i, c in enumerate(FORMAT_COLUMNS)})
        row["notes"] = f.get("notes", "")
        rows.append(row)
    df = _text_df(pd.DataFrame(rows, columns=["name", *FORMAT_COLUMNS, "notes"]))
    options = list(FIELD_LABELS.values())
    edited = st.data_editor(
        df,
        num_rows="dynamic",
        width="stretch",
        key=f"ed_formats_{_ver()}",
        column_config={
            "name": st.column_config.TextColumn("Fournisseur / format", required=True),
            **{c: st.column_config.SelectboxColumn(
                f"{i}ᵉ nombre en fin de ligne" if i > 1 else "1ᵉʳ nombre en fin de ligne", options=options,
                help="Ce que représente ce nombre, en lisant la fin de la ligne article de gauche à droite.",
            ) for i, c in enumerate(FORMAT_COLUMNS, start=1)},
            "notes": st.column_config.TextColumn("Notes"),
        },
    )
    if st.button("💾 Enregistrer les formats", key="save_formats"):
        formats, errors = validate_formats(edited)
        templates = [f for f in doc.get("formats", []) if _is_template(f)]
        names = {str(t.get("name", "")).lower() for t in templates}
        errors += [f"« {f['name']} » existe déjà comme modèle par en-têtes" for f in formats if f["name"].lower() in names]
        if errors:
            _show_errors(errors)
        elif _save(DOC_FORMATS, {**doc, "formats": [*formats, *templates]}, doc):
            st.rerun(scope="fragment")


HELP_MD = """
### Comment l'application fabrique un produit Shopify

Sur chaque ligne de facture, l'application lit la **désignation** et la **découpe en morceaux** : chaque morceau
reconnu va dans sa case du CSV Shopify, et **ce qui reste devient le nom du produit**.
Les onglets suivants sont rangés **dans l'ordre où les morceaux sont reconnus**.

**Exemple** — la facture dit : `Santa Cruz Screaming Hand Tee Black M`

| Morceau | Reconnu grâce à l'onglet | Devient dans Shopify |
|---|---|---|
| **Santa Cruz** | 1 · Marques | la marque (*Vendor*) |
| **Tee** | 2 · Types de produit | le mot « T-Shirt » dans le titre, + sa catégorie (5) et sa collection (6) |
| **M** | 3 · Tailles | la variante *Taille = M* |
| **Black** | 4 · Couleurs | la fin du titre |
| Screaming Hand | *(reste)* | le nom du produit |

➡️ Résultat : **Santa Cruz T-Shirt - Screaming Hand - Black**, variante Taille **M**.

---

### Les onglets en une phrase

- **0 · Modèles de facture** — comment *lire le PDF* d'un fournisseur (quelle colonne contient la quantité, le prix…).
  Le plus simple : déposez une facture et utilisez « ➕ Créer un modèle fournisseur » sous le PDF.
- **1 · Marques** — la liste de vos marques. Écrivez-les **comme vous voulez les voir dans Shopify** (« Santa Cruz »).
- **2 · Types de produit** — les familles de produits (t-shirt, roue, board, chaussure…). Pour chacune : les **mots qui
  la font reconnaître**, le **mot écrit dans le titre**, et les **variantes** créées (taille, largeur, dureté…).
  ⚠️ Les lignes sont testées **de haut en bas : la première qui correspond gagne.**
- **3 · Tailles (US → FR)** — sert **uniquement** aux factures en tailles américaines/anglaises : traduit « 9 » en « 42 ».
  Les tailles en lettres (S, M, L…) ne sont jamais traduites.
- **4 · Couleurs** — les mots de couleur, placés à la fin du titre. Une autre couleur = un **autre produit** (pas une variante).
- **5 · Type Shopify** — pour chaque type de produit, la **catégorie exacte** de votre boutique Shopify (*Product Type*).
- **6 · Collections** — pour chaque type de produit, la **collection** à ajouter (dans la colonne *Tags*).

---

### Les règles à retenir

1. **Majuscules ignorées, mots entiers** : « vans » trouve « VANS », mais « cap » ne trouve pas « capuche ».
2. **Même titre = même produit** : les lignes qui donnent le même titre sont regroupées et leurs tailles deviennent des variantes.
3. **Ce qui n'est pas reconnu reste dans le titre** (ex. « (EU) », « ADULT »). Corrigez-le à l'étape 2, ou ajoutez le
   mot manquant dans la bonne liste.
4. Chaque produit reçoit toujours le tag **nouveautés**.
5. Le **code du type** (ex. `tee`) relie les onglets 2, 3, 5 et 6 : utilisez exactement le même partout.

---

### Par où commencer

1. **2 · Types de produit** : ajoutez les mots qu'utilisent vos fournisseurs (Hood, Chukka, Half Cab…).
2. **5 · Type Shopify** : recopiez vos catégories Shopify exactes (Shopify → Produits → filtre « Type de produit »).
3. **6 · Collections** : listez vos collections, puis associez-les aux types.
4. **1 · Marques** et **4 · Couleurs** : complétez au fil des factures.
5. **3 · Tailles** : vérifiez les correspondances de vos marques de chaussures.

Pour savoir ce qui manque : l'onglet **📋 Diagnostic configuration**, et les **alertes ⚠️** de l'étape 2
(« Marque non reconnue », « Type de produit non reconnu »…).

---

### Petit lexique Shopify

| Mot | Signification |
|---|---|
| **Vendor** | la marque du produit |
| **Product Type** (*Type*) | la catégorie du produit dans votre boutique |
| **Tags** | des étiquettes ; une collection automatique regroupe les produits qui portent son tag |
| **Variante** | une déclinaison d'un même produit (taille, largeur…), avec son propre stock et SKU |
"""


def render_business_config() -> None:
    """Consultation/édition des fichiers de config métier (affiché dans Paramètres ⚙️), dans l'ordre du traitement."""
    t_help, t_formats, t_brands, t_types, t_sizes, t_colors, t_map, t_coll, t_raw = st.tabs(
        ["❓ Aide", "0 · Modèles de facture", "1 · Marques", "2 · Types de produit", "3 · Tailles (US → FR)",
         "4 · Couleurs", "5 · Type Shopify", "6 · Collections", "JSON brut"]
    )
    with t_help:
        st.markdown(HELP_MD)
    with t_formats:
        _formats_tab()
    with t_brands:
        _list_editor(
            "Marques", "product_rules.json", "brands", "Marque",
            intro="Si une de ces marques est trouvée dans la désignation, elle devient le **Vendor** du produit et le début "
            "du titre. Écrivez-la comme dans Shopify ; les majuscules de la facture n'ont pas d'importance.",
            column_title="Marque (telle qu'affichée dans Shopify)",
        )
    with t_types:
        _types_tab()
    with t_sizes:
        _sizes_tab()
    with t_colors:
        _list_editor(
            "Couleurs", "product_rules.json", "colors", "Couleur",
            intro="Si un de ces mots est trouvé dans la désignation, il est placé à la **fin du titre** (« … - Black »). "
            "Écrivez-les comme sur les factures (souvent en anglais). Un seul mot par ligne.",
            column_title="Mot de couleur (tel qu'écrit sur les factures)",
        )
    with t_map:
        _mapping_tab()
    with t_coll:
        _collections_tab()
    with t_raw:
        name = st.selectbox("Fichier", ["product_rules.json", "product_type_mapping.json", "collections.json", DOC_FORMATS, DOC_SIZES])
        alpha = st.toggle("🔤 Afficher les clés par ordre alphabétique", key="raw_sort", help="Affichage seulement : le fichier n'est pas modifié.")
        text = json.dumps(load_json(name), ensure_ascii=False, indent=2, sort_keys=alpha)
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
