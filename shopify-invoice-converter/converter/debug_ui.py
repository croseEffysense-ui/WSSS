"""Mode debug : montre ce que l'application lit dans un PDF (texte, positions, colonnes, lignes, méthodes testées)
et permet d'enregistrer un modèle de facture fournisseur à partir de ce PDF."""
from __future__ import annotations

import re

import pandas as pd
import streamlit as st

from .analysis import METHOD_TAIL, Analysis
from .config import load_json, save_json
from .formats import TABLE_FIELDS, upsert_template, validate_table
from .models import items_to_df
from .storage import DOC_FORMATS, StorageError
from .table_parser import (
    ROLE_CONT, ROLE_FOOTER, ROLE_HEADER, ROLE_IGNORED, ROLE_ITEM, ROLE_OUTSIDE, Line, cells, header_columns, parse_pages,
)

ROLE_ICONS = {
    ROLE_HEADER: "🟨", ROLE_ITEM: "🟩", ROLE_CONT: "🟦", ROLE_FOOTER: "🟥", ROLE_IGNORED: "▫️", ROLE_OUTSIDE: "⬜",
}
NONE = "—"


def render_debug(a: Analysis, key: str) -> None:
    """Panneau de debug complet pour une analyse de PDF. `key` rend les widgets uniques par document."""
    st.subheader("🐞 Debug · ce que l'application lit dans ce PDF")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Pages", len(a.pages) or "?")
    m2.metric("Mots positionnés", sum(len(line.words) for page in a.pages for line in page))
    m3.metric("Méthodes testées", len(a.candidates))
    m4.metric("Articles retenus", len(a.items))
    st.caption(f"Texte brut lu par : {a.text_source or '—'}")
    for note in a.notes:
        st.caption(f"ℹ️ {note}")

    t_methods, t_cols, t_lines, t_cells, t_text, t_words, t_model = st.tabs(
        ["Méthodes testées", "Colonnes", "Lignes analysées", "Cellules par article", "Texte brut", "Mots (x, y)", "💾 Créer un modèle"]
    )
    with t_methods:
        _methods_tab(a, key)
    with t_cols:
        _columns_tab(a)
    with t_lines:
        _lines_tab(a, key)
    with t_cells:
        trace = a.table_trace
        if trace and trace.cells:
            st.caption("Texte lu sous chaque en-tête, sur la ligne de chaque article (avant conversion en nombres).")
            st.dataframe(pd.DataFrame(trace.cells), width="stretch", hide_index=True)
        else:
            st.info("Aucun article lu par colonnes.")
    with t_text:
        st.caption("Texte utilisé par les formats « colonnes de fin de ligne ».")
        st.code(a.text or "(vide)", language=None)
        st.download_button("📥 Télécharger le texte", (a.text or "").encode("utf-8"), file_name="texte_pdf.txt", key=f"dbg-dl-{key}")
    with t_words:
        _words_tab(a, key)
    with t_model:
        _model_tab(a, key)


def _methods_tab(a: Analysis, key: str) -> None:
    st.caption(
        "En détection automatique, la méthode retenue est celle qui reconnaît le fournisseur (texte « identifiant » "
        "d'un modèle), puis celle qui donne le plus de lignes sans alerte."
    )
    rows = [
        {
            "Retenu": "✅" if c is a.chosen else "",
            "Format": c.name,
            "Méthode": c.method,
            "Articles": len(c.items),
            "Sans alerte": c.ok,
            "Fournisseur reconnu": "oui" if c.supplier_match else "",
            "Lecture": c.describe(),
        }
        for c in sorted(a.candidates, key=lambda c: c.score, reverse=True)
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    with_items = [c for c in a.candidates if c.items]
    if with_items:
        names = [c.name for c in with_items]
        default = names.index(a.chosen.name) if a.chosen and a.chosen.name in names else 0
        pick = st.selectbox("Voir les lignes extraites par", names, index=default, key=f"dbg-pick-{key}")
        st.dataframe(items_to_df(with_items[names.index(pick)].items), width="stretch", hide_index=True)


def _columns_tab(a: Analysis) -> None:
    trace = a.table_trace
    if not trace or not trace.columns:
        st.info(trace.problem if trace and trace.problem else "Lecture par colonnes indisponible pour ce PDF.")
        return
    if a.chosen and a.chosen.method == METHOD_TAIL:
        st.caption("Le format retenu n'utilise pas les colonnes ; voici ce qu'aurait donné la détection automatique des en-têtes.")
    st.caption(
        "Chaque nombre est rangé sous l'en-tête le plus proche (aligné à droite ou centré). "
        "Les colonnes sans champ (Remise, TVA, Taille…) sont lues mais ignorées."
    )
    st.dataframe(
        pd.DataFrame([
            {
                "En-tête sur la facture": c.label,
                "Champ": TABLE_FIELDS.get(c.field, "— ignorée"),
                "x début": round(c.x0, 1),
                "x fin": round(c.x1, 1),
            }
            for c in trace.columns
        ]),
        width="stretch",
        hide_index=True,
    )
    if trace.problem:
        st.warning(trace.problem)


def _lines_tab(a: Analysis, key: str) -> None:
    trace = a.table_trace
    if not trace or not trace.lines:
        st.info("Aucune ligne positionnée (PDF scanné ou illisible ?).")
        return
    st.caption(" · ".join(f"{icon} {role}" for role, icon in ROLE_ICONS.items()))
    show_outside = st.toggle("Afficher aussi les lignes hors tableau (adresses, pied de page…)", key=f"dbg-out-{key}")
    rows = [
        {"Page": lt.page, "y": lt.y, "Rôle": f"{ROLE_ICONS.get(lt.role, '')} {lt.role}", "Texte": lt.text, "Pourquoi": lt.detail}
        for lt in trace.lines
        if show_outside or lt.role != ROLE_OUTSIDE
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, height=min(35 * len(rows) + 40, 600))


def _words_tab(a: Analysis, key: str) -> None:
    if not a.pages:
        st.info("Positions des mots indisponibles.")
        return
    page = st.selectbox("Page", range(1, len(a.pages) + 1), key=f"dbg-page-{key}")
    st.caption("Coordonnées en points PDF (x de gauche à droite, y de haut en bas). Utile pour comprendre un alignement.")
    st.dataframe(
        pd.DataFrame([
            {"y": round(w.top, 1), "x début": round(w.x0, 1), "x fin": round(w.x1, 1), "Mot": w.text}
            for line in a.pages[page - 1]
            for w in line.words
        ]),
        width="stretch",
        hide_index=True,
        height=400,
    )


def _header_choices(a: Analysis) -> tuple[list[Line], int, int]:
    """Lignes candidates pour l'en-tête (sans chiffres, au moins 2 cellules) + choix par défaut (index, nb de lignes)."""
    lines = [line for page in a.pages for line in page if not re.search(r"\d", line.text) and len(cells(line)) >= 2]
    trace = a.table_trace
    header = [lt for lt in (trace.lines if trace else []) if lt.role == ROLE_HEADER]
    default, size = 0, 1
    if header:
        first = header[0]
        for i, line in enumerate(lines):
            if line.page == first.page and round(line.top, 1) == first.y:
                default, size = i, len([lt for lt in header if lt.page == first.page])
                break
    return lines, default, min(max(size, 1), 3)


def _model_tab(a: Analysis, key: str) -> None:
    st.markdown(
        "Enregistrez la façon de lire **ce fournisseur** : choisissez la ligne d'en-tête du tableau, "
        "associez chaque information à une colonne, vérifiez l'aperçu puis enregistrez. "
        "Le modèle sera ensuite proposé dans « Format fournisseur » et utilisé par la détection automatique."
    )
    candidates, default, size = _header_choices(a)
    if not candidates:
        st.info("Aucune ligne d'en-tête possible (PDF scanné ?) : un modèle par en-têtes ne peut pas être créé pour ce document.")
        return
    c_line, c_n = st.columns([4, 1])
    idx = c_line.selectbox(
        "Ligne d'en-tête du tableau",
        range(len(candidates)),
        index=default,
        format_func=lambda i: f"p{candidates[i].page} · {candidates[i].text[:110]}",
        key=f"dbg-hdr-{key}",
    )
    n = c_n.number_input("Lignes d'en-tête", 1, 3, value=size, key=f"dbg-hdrn-{key}", help="Ex. 2 si « Prix » est au-dessus de « Net »")
    page = a.pages[candidates[idx].page - 1]
    start = next(i for i, line in enumerate(page) if line is candidates[idx])
    columns = header_columns(page[start : start + int(n)])
    labels = [c.label for c in columns]

    st.caption("Pour chaque information, l'en-tête de colonne correspondant sur la facture (— = absente de la facture).")
    mapping: dict[str, str] = {}
    grid = st.columns(4)
    for i, (fld, label) in enumerate(TABLE_FIELDS.items()):
        auto = next((c.label for c in columns if c.field == fld), NONE)
        options = [NONE, *labels]
        choice = grid[i % 4].selectbox(label, options, index=options.index(auto), key=f"dbg-map-{key}-{fld}-{idx}-{n}")
        if choice != NONE:
            mapping[fld] = choice

    errors = [e for e in validate_table("modèle", mapping)]
    if errors:
        for e in errors:
            st.warning(e)
        return
    items, trace = parse_pages(a.pages, mapping)
    st.markdown(f"**Aperçu avec ce modèle : {len(items)} article(s)**, dont {sum(1 for i in items if not i.warnings)} sans alerte")
    if trace.problem:
        st.warning(trace.problem)
    if items:
        st.dataframe(items_to_df(items), width="stretch", hide_index=True)

    c_name, c_match = st.columns(2)
    name = c_name.text_input("Nom du fournisseur", key=f"dbg-name-{key}", placeholder="ex. Adidas")
    match = c_match.text_input(
        "Texte qui identifie ce fournisseur (facultatif, recommandé)",
        key=f"dbg-match-{key}",
        placeholder="ex. adidas France, ou son n° de TVA",
        help="Si ce texte figure dans un PDF, la détection automatique utilise ce modèle en priorité. "
        "Choisissez un texte présent sur toutes ses factures (nom de la société, n° de TVA…).",
    )
    if match and match.strip() and match.strip().lower() not in a.text.lower():
        st.caption("⚠️ Ce texte n'apparaît pas dans ce PDF : le fournisseur ne sera pas reconnu automatiquement.")
    if st.button("💾 Enregistrer le modèle", type="primary", key=f"dbg-save-{key}", disabled=not items):
        problems = validate_table(name, mapping)
        if problems:
            for p in problems:
                st.error(p)
            return
        try:
            doc = load_json(DOC_FORMATS)
            replaced = any(str(f.get("name", "")).strip().lower() == name.strip().lower() for f in doc.get("formats", []))
            save_json(DOC_FORMATS, upsert_template(doc, name, mapping, match))
        except (OSError, ValueError, StorageError) as exc:
            st.error(f"Enregistrement impossible : {exc}")
            return
        st.toast(f"Modèle « {name.strip()} » {'mis à jour' if replaced else 'enregistré'} ✅")
        st.rerun()
