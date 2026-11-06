"""Interface Streamlit : conversion facture/BL -> CSV Shopify + rapprochement PO/BL."""
from __future__ import annotations

import logging
import os
from datetime import date

import pandas as pd
import streamlit as st

from converter.analysis import AUTO, AUTO_TABLE, METHOD_TEMPLATE, Analysis, analyze
from converter.config import CONFIG_DIR, config_health, get_store, load_formats, load_json, set_store, template_columns
from converter.config_ui import render_business_config
from converter.debug_ui import render_debug, render_supplier_section
from converter.diagnostics import secrets_toml_problem
from converter.diagnostics_ui import render_diagnostics
from converter.logs import setup_logging
from converter.models import LineItem, df_to_items, items_to_df
from converter.normalizer import normalize_item
from converter.pdf_parser import PdfExtractionError
from converter.reconciliation import STATUS_OK, reconcile
from converter.shopify_builder import build_dataframe
from converter.sizes import DOC_SIZES, SIZE_SYSTEMS, chart_names, charts_from_doc
from converter.storage import LocalJsonStore, StorageError, build_store

SIZES_FROM_FORMAT = "Selon le format fournisseur"
SIZES_FR = "FR / EU (aucune conversion)"
NO_CHART = "—"

PREVIEW_COLUMNS = [
    "Title", "URL handle", "Vendor", "Type", "Tags", "SKU", "Barcode",
    "Option1 name", "Option1 value", "Option2 name", "Option2 value",
    "Price", "Cost per item", "Inventory quantity",
]

setup_logging()  # idempotent : console + fichier logs/ + tampon mémoire affiché dans l'interface
log = logging.getLogger("converter.app")

st.set_page_config(page_title="Wall Street Skateshop Shopify Tools", page_icon="🛍️", layout="wide")


# ----------------------------- Helpers -----------------------------
def _read_secrets() -> tuple[dict, str | None]:
    """Lit les secrets Streamlit. Retourne (secrets, message d'erreur). Ne masque plus les erreurs."""
    secrets: dict = {}
    try:
        for key in ("gcp_service_account", "config_sheet"):
            if key in st.secrets:
                secrets[key] = dict(st.secrets[key])
    except FileNotFoundError:
        # Streamlit lève la MÊME erreur pour « fichier absent » et « TOML invalide » : on les distingue.
        problem = secrets_toml_problem()
        if problem:
            log.error("secrets.toml présent mais invalide : %s", problem)
            return {}, f"secrets.toml invalide — {problem}"
        log.info("Aucun fichier secrets.toml trouvé (mode local si aucune variable d'environnement)")
    except Exception as exc:  # noqa: BLE001 - TOML invalide, etc. : on le journalise ET on l'affiche
        log.error("Lecture des secrets Streamlit impossible : %s: %s", type(exc).__name__, exc, exc_info=True)
        return {}, f"{type(exc).__name__}: {exc}"
    log.info("Secrets Streamlit lus : sections %s", sorted(secrets))
    return secrets, None


@st.cache_resource(show_spinner="Connexion à la base de configuration…")
def _init_store():
    """Crée le backend de config une seule fois par processus. Retourne (store | None, message d'erreur | None)."""
    secrets, secrets_error = _read_secrets()
    if secrets_error:
        return None, f"Secrets illisibles : {secrets_error}"
    try:
        store = build_store(secrets, os.environ, LocalJsonStore(CONFIG_DIR))
        log.info("Backend de configuration actif : %s", store.description)
        return store, None
    except StorageError as exc:
        log.error("Échec de connexion à la base de configuration : %s", exc)
        return None, str(exc)


@st.cache_data(show_spinner=False)
def _analyze(file_bytes: bytes, choice: str, profiles: dict[str, list[str]], templates: dict[str, dict]) -> Analysis:
    """Analyse complète du PDF (toutes méthodes + traces pour le mode debug), mise en cache par contenu."""
    return analyze(file_bytes, choice, profiles, templates)


def _parse_document(file_bytes: bytes, choice: str) -> list[LineItem]:
    """Lignes du document. Lève PdfExtractionError avec un message lisible."""
    analysis = _analyze(file_bytes, choice, profiles, templates)
    if analysis.error:
        raise PdfExtractionError(analysis.error)
    return analysis.items


def _size_settings(chosen, key: str) -> tuple[str, str]:
    """(système des tailles du document, tableau par défaut) : d'après le modèle fournisseur, modifiables ici."""
    template = templates.get(chosen.name, {}) if chosen.method == METHOD_TEMPLATE else {}
    c_sys, c_chart = st.columns(2)
    choice = c_sys.selectbox(
        "Tailles du document",
        [SIZES_FROM_FORMAT, SIZES_FR, *SIZE_SYSTEMS],
        key=f"sizes-{key}",
        help="Tailles US/UK converties en tailles françaises avec le tableau associé au type de produit "
        "(Paramètres ⚙️ → Configuration application → Tailles). Une taille écrite « US 9 » est toujours convertie ; S, M, L… jamais.",
    )
    system = {SIZES_FROM_FORMAT: template.get("size_system", ""), SIZES_FR: ""}.get(choice, choice)
    options = [NO_CHART, *size_chart_names]
    default = template.get("size_chart", "")
    chart = c_chart.selectbox(
        "Tableau de tailles si le type de produit n'en a pas",
        options,
        index=options.index(default) if default in options else 0,
        key=f"chart-{key}",
        disabled=not system,
        help="Ex. « Chaussures » pour un fournisseur de chaussures dont les désignations ne disent pas « shoe ».",
    )
    if system:
        st.caption(f"📏 Tailles **{system}** converties en tailles françaises à l'étape 3 (colonne Option1 value du CSV).")
    return system, "" if chart == NO_CHART else chart


def _guess_column(columns: list[str], keywords: tuple[str, ...], fallback: int) -> int:
    for idx, col in enumerate(columns):
        if any(k in str(col).lower() for k in keywords):
            return idx
    return min(fallback, len(columns) - 1)


# ----------------------------- Réglages (conservés pendant la session) -----------------------------
# Les réglages vivent dans la fenêtre « Paramètres ⚙️ » : leurs valeurs sont gardées dans la session (clés opt_*)
# pour survivre à la fermeture de la fenêtre.
ss = st.session_state
for _key, _default in {"opt_debug": False, "opt_coef": 2.4, "opt_tva": 20.0, "opt_template": None, "opt_template_name": ""}.items():
    ss.setdefault(_key, _default)

store, store_error = _init_store()
set_store(store)  # None -> fichiers JSON locaux de secours


def _general_settings() -> None:
    ss.opt_debug = st.toggle(
        "🐞 Mode debug",
        value=ss.opt_debug,
        key="w_debug",
        help="Affiche sous le PDF déposé tout ce que l'application en extrait (texte, colonnes, lignes, méthodes testées).",
    )
    st.divider()
    st.markdown("**💶 Prix de vente de secours**")
    st.caption("Utilisés uniquement quand le prix public TTC est absent du document : prix = coût × coefficient × (1 + TVA).")
    c_coef, c_tva = st.columns(2)
    ss.opt_coef = c_coef.number_input("Coefficient multiplicateur", 1.0, 10.0, float(ss.opt_coef), 0.1, key="w_coef")
    ss.opt_tva = c_tva.number_input("Taux de TVA (%)", 0.0, 30.0, float(ss.opt_tva), 0.5, key="w_tva")
    st.divider()
    st.markdown("**🧩 Template Shopify (avancé)**")
    st.caption("Fichier CSV dont les colonnes (et leur ordre) définissent le CSV généré. Par défaut : le template du dépôt.")
    upload = st.file_uploader("Remplacer le template", type=["csv"], key="w_template")
    if upload is not None:
        ss.opt_template, ss.opt_template_name = upload.getvalue(), upload.name
    if ss.opt_template:
        try:
            st.success(f"Template personnalisé : **{ss.opt_template_name}** ({len(template_columns(ss.opt_template))} colonnes)")
        except (OSError, ValueError) as exc:
            st.error(f"Template illisible : {exc}")
        if st.button("↩️ Revenir au template par défaut", key="w_template_reset"):
            ss.opt_template, ss.opt_template_name = None, ""
            st.rerun()
    else:
        st.caption(f"Template utilisé : template du dépôt ({len(template_columns())} colonnes).")


def _storage_settings() -> None:
    if store_error:
        st.error(f"Google Sheets indisponible : {store_error}")
        st.caption("Mode de secours : fichiers JSON locaux. Le diagnostic ci-dessous localise le blocage.")
        if st.button("🔁 Réessayer la connexion", help="Après correction des secrets ou du partage du Sheet"):
            _init_store.clear()
            log.info("Nouvelle tentative de connexion demandée par l'utilisateur")
            st.rerun()
    active = get_store()
    if active.url:
        st.success(f"Connecté à {active.description}")
        c_open, c_reload = st.columns(2)
        c_open.link_button("📄 Ouvrir le Google Sheet", active.url, width="stretch")
        if c_reload.button("🔄 Recharger", width="stretch", help="Relit le Google Sheet (utile après une modification directe)"):
            active.refresh()
            st.rerun()
    elif not store_error:
        st.info(f"{active.description}")
        st.caption("Pour partager la config en équipe, connectez un Google Sheet (voir README).")
    st.divider()
    secrets, secrets_error = _read_secrets()
    render_diagnostics(secrets, dict(os.environ), secrets_error)


def _health_settings() -> None:
    st.caption(
        "Points de la configuration métier à compléter ou à corriger (marques, types, Product Types, collections, "
        "modèles de facture, tailles). Corrigez-les dans l'onglet « 🧾 Configuration application » : ce diagnostic "
        "se met à jour à chaque enregistrement."
    )
    try:
        health = config_health(
            load_json("product_rules.json"), load_json("product_type_mapping.json"), load_json("collections.json"),
            load_json("supplier_formats.json"), load_json(DOC_SIZES),
        )
    except (OSError, ValueError) as exc:
        st.error(f"Configuration illisible : {exc}")
        return
    if not health:
        st.success("Configuration complète ✅")
        return
    st.warning(f"**{len(health)} point(s) de configuration à compléter**")
    st.markdown("\n".join(f"- {h}" for h in health))


@st.dialog("Paramètres ⚙️", width="large", on_dismiss="rerun")
def settings_dialog() -> None:
    t_general, t_health, t_storage, t_business = st.tabs(
        ["⚙️ Général", "📋 Diagnostic configuration", "📚 Base de donnée Sheet", "🧾 Configuration application"]
    )
    with t_general:
        _general_settings()
    with t_health:
        _health_settings()
    with t_storage:
        _storage_settings()
    with t_business:
        render_business_config()


debug, coef, tva = ss.opt_debug, ss.opt_coef, ss.opt_tva

# ----------------------------- En-tête -----------------------------
# Avant le chargement de la config : Paramètres ⚙️ reste accessible même si la config est invalide.
c_title, c_buttons = st.columns([3, 1], vertical_alignment="center")
c_title.title("Wall Street Skateshop Shopify Tools 🛍️🛠️")
with c_buttons:
    if st.button(
        "Paramètres ⚙️", type="primary", width="stretch",
        help="Mode debug, prix de secours, template Shopify, diagnostic de la configuration, base de donnée Sheet "
        "(Google Sheets), modèles de facture, marques, types, tailles, collections…",
    ):
        settings_dialog()
    with st.popover("❓ Aide", width="stretch"):
        st.markdown(
            "1. **Déposez** la facture ou le BL (PDF texte).\n"
            "2. **Vérifiez** les lignes extraites ; corrigez dans le tableau si besoin.\n"
            "3. **Téléchargez** le CSV et importez-le dans Shopify.\n\n"
            "Les alertes ⚠️ indiquent ce qui mérite un coup d'œil (marque inconnue, total incohérent…).\n\n"
            "Réglages, configuration et diagnostic : bouton **Paramètres ⚙️** juste au-dessus."
        )
st.caption("Déposez un PDF, vérifiez les lignes, téléchargez le CSV prêt à importer.")

try:
    rules = load_json("product_rules.json")
    type_mapping = load_json("product_type_mapping.json")
    collections = load_json("collections.json")
    formats_doc = load_json("supplier_formats.json")
    sizes_doc = load_json(DOC_SIZES)
    size_charts, size_chart_names = charts_from_doc(sizes_doc), chart_names(sizes_doc)
    profiles, templates, _format_problems = load_formats()
except (OSError, ValueError) as exc:  # inclut FileNotFoundError et StorageError
    log.error("Chargement de la configuration impossible : %s", exc, exc_info=True)
    st.error(f"Configuration invalide : {exc}. Corrigez-la via Paramètres ⚙️ (en haut à droite).")
    st.stop()
try:
    columns = template_columns(ss.opt_template)
except (OSError, ValueError) as exc:  # template personnalisé illisible : on revient à celui du dépôt
    log.warning("Template Shopify personnalisé illisible (%s) : template du dépôt utilisé", exc)
    st.warning(f"Template Shopify personnalisé illisible ({exc}) : le template du dépôt est utilisé.")
    ss.opt_template, ss.opt_template_name = None, ""
    columns = template_columns()

if store_error:
    st.error("Google Sheets indisponible : mode fichiers locaux. Détail dans Paramètres ⚙️ → Base de donnée Sheet.")


# ----------------------------- Onglet 1 : conversion -----------------------------
def render_convert() -> None:
    st.subheader("Étape 1 · Déposez votre document")
    col_up, col_fmt = st.columns([3, 2])
    pdf = col_up.file_uploader("Facture / BL fournisseur (PDF)", type=["pdf"], key="invoice")
    choice = col_fmt.selectbox(
        "Format fournisseur",
        [AUTO, *templates, *profiles, AUTO_TABLE],
        help="Modèles et formats enregistrés dans Paramètres ⚙️ → Configuration application → Modèles de facture, plus la lecture "
        "automatique des tableaux. « Détection automatique » teste tout et retient le plus cohérent.",
    )
    if not pdf:
        st.info("👆 Déposez un PDF pour commencer. Le fichier reste sur cette session et n'est pas conservé.")
        return

    try:
        with st.spinner("Analyse du PDF…"):
            analysis = _analyze(pdf.getvalue(), choice, profiles, templates)
    except Exception as exc:  # noqa: BLE001 - dernier filet de sécurité UI
        log.error("Erreur inattendue pendant l'analyse de %s : %s", pdf.name, exc, exc_info=True)
        st.error(f"Erreur inattendue pendant l'analyse : {exc}")
        return

    if analysis.error:
        st.error(analysis.error)
    render_supplier_section(analysis, templates, f"{pdf.name}-{pdf.size}")
    if debug:
        with st.expander("🐞 Debug de l'extraction", expanded=bool(analysis.error)):
            render_debug(analysis, key=f"{pdf.name}-{pdf.size}")
    if analysis.error:
        return

    chosen = analysis.chosen
    st.subheader("Étape 2 · Vérifiez les lignes extraites")
    st.caption(
        f"Format utilisé : **{chosen.name}** ({chosen.describe()}) · Modifiez une cellule en double-cliquant, "
        "ajoutez une ligne avec ➕ en bas, supprimez en cochant la ligne puis 🗑️."
    )
    edited = st.data_editor(
        items_to_df(analysis.items),
        num_rows="dynamic",
        width="stretch",
        key=f"editor-{pdf.name}-{pdf.size}-{chosen.name}",
        disabled=["Alertes"],
        column_config={
            "SKU": st.column_config.TextColumn(required=True),
            "Description": st.column_config.TextColumn(width="large"),
            "Qté": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
            "PU HT (brut)": st.column_config.NumberColumn(format="%.2f €"),
            "PU Net": st.column_config.NumberColumn(format="%.2f €"),
            "Total": st.column_config.NumberColumn(format="%.2f €"),
            "Prix public TTC": st.column_config.NumberColumn(format="%.2f €"),
            "Taille": st.column_config.TextColumn(help="Taille telle qu'écrite sur la facture (convertie en FR à l'étape 3 si besoin)"),
            "Alertes": st.column_config.TextColumn(help="Détectées à l'extraction (non modifiables)"),
        },
    )

    size_system, size_chart = _size_settings(chosen, f"{pdf.name}-{pdf.size}")
    try:
        products = [
            normalize_item(i, rules, type_mapping, collections, coef, tva, size_system, size_chart, size_charts)
            for i in df_to_items(edited)
        ]
        df = build_dataframe(products, columns)
    except (KeyError, ValueError) as exc:
        st.error(f"Impossible de générer le CSV : {exc}")
        return
    if df.empty:
        st.warning("Le tableau est vide : ajoutez au moins une ligne.")
        return

    issues = [{"SKU": p.sku, "Alerte": w} for p in products for w in p.warnings]
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Lignes lues", len(products))
    m2.metric("Produits Shopify", df["URL handle"].nunique())
    m3.metric("Unités", sum(p.qty for p in products))
    m4.metric("Valeur d'achat", f"{sum((p.cost or 0) * p.qty for p in products):,.2f} €".replace(",", " "))
    m5.metric("Alertes", len(issues), delta="à vérifier" if issues else "aucune", delta_color="inverse" if issues else "off")

    if issues:
        with st.expander(f"⚠️ {len(issues)} alerte(s) à vérifier", expanded=len(issues) <= 15):
            st.dataframe(pd.DataFrame(issues), width="stretch", hide_index=True)

    st.subheader("Étape 3 · Téléchargez le CSV")
    show_all = st.toggle(f"Afficher les {len(columns)} colonnes du template", value=False)
    preview = df if show_all else df[[c for c in PREVIEW_COLUMNS if c in df.columns]]
    st.dataframe(preview, width="stretch", hide_index=True)
    st.download_button(
        "📥 Télécharger le CSV Shopify",
        data=df.to_csv(index=False).encode("utf-8"),
        file_name=f"shopify_import_{date.today():%Y-%m-%d}.csv",
        mime="text/csv",
        type="primary",
    )


# ----------------------------- Onglet 2 : rapprochement -----------------------------
def render_reconcile() -> None:
    st.markdown("Compare les quantités du **Bon de Livraison** avec l'ordre du **Bon de Commande**.")
    c1, c2 = st.columns(2)
    po_file = c1.file_uploader("1 · Bon de commande (PDF)", type=["pdf"], key="po")
    bl_file = c2.file_uploader("2 · Bon de livraison (PDF ou CSV)", type=["pdf", "csv"], key="bl")
    with st.expander("Options avancées"):
        po_choice = st.selectbox("Format fournisseur du PO", [AUTO, *templates, *profiles, AUTO_TABLE], key="po_fmt")
        bl_choice = st.selectbox("Format fournisseur du BL (PDF)", [AUTO, *templates, *profiles, AUTO_TABLE], key="bl_fmt")

    if not (po_file and bl_file):
        st.info("👆 Déposez les deux documents pour lancer le rapprochement.")
        return

    try:
        po_items = _parse_document(po_file.getvalue(), po_choice)
        if bl_file.name.lower().endswith(".csv"):
            bl_df = pd.read_csv(bl_file, dtype=str)
            cols = list(bl_df.columns)
            a, b = st.columns(2)
            sku_col = a.selectbox("Colonne SKU", cols, index=_guess_column(cols, ("sku", "ref", "réf"), 0))
            qty_col = b.selectbox("Colonne quantité", cols, index=_guess_column(cols, ("qt", "qty", "quant"), 1))
            bl_items = [
                LineItem(sku=str(r[sku_col]).strip(), qty=int(float(r[qty_col] or 0))) for _, r in bl_df.iterrows()
            ]
        else:
            bl_items = _parse_document(bl_file.getvalue(), bl_choice)
    except PdfExtractionError as exc:
        st.error(str(exc))
        return
    except (ValueError, KeyError) as exc:
        st.error(f"Fichier BL invalide : {exc}")
        return

    result = reconcile(po_items, bl_items)
    (st.success if result.ok else st.warning)(result.header)

    nb_ok = sum(1 for r in result.rows if r["Statut"] == STATUS_OK)
    m1, m2, m3 = st.columns(3)
    m1.metric("Lignes du PO", len(po_items))
    m2.metric("Conformes", nb_ok)
    m3.metric("Écarts", len(result.discrepancies), delta_color="inverse", delta="à traiter" if result.discrepancies else "aucun")

    st.dataframe(pd.DataFrame(result.rows), width="stretch", hide_index=True)

    left, right = st.columns(2)
    with left:
        st.markdown("**Quantités à copier (ordre du PO)**")
        st.code(result.quantities_text(), language=None)
        st.download_button(
            "📥 Télécharger les quantités (.txt)", result.quantities_text().encode("utf-8"),
            file_name="quantites.txt", mime="text/plain",
        )
    with right:
        if result.discrepancies:
            st.markdown("**Détail des écarts**")
            st.markdown("\n".join(f"- {d}" for d in result.discrepancies))
        st.caption(f"Total livré : {sum(q for q in result.quantities if q is not None)}")


# ----------------------------- Mise en page -----------------------------
tab_convert, tab_reconcile = st.tabs(["1️⃣ Conversion → CSV Shopify", "2️⃣ Rapprochement PO / BL"])
with tab_convert:
    render_convert()
with tab_reconcile:
    render_reconcile()
