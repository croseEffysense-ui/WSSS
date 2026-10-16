"""Interface Streamlit : conversion facture/BL -> CSV Shopify + rapprochement PO/BL."""
from __future__ import annotations

import io
from datetime import date

import pandas as pd
import streamlit as st

from converter.config import PROFILES, config_health, load_json, template_columns
from converter.config_ui import config_dialog
from converter.models import LineItem, df_to_items, items_to_df
from converter.normalizer import normalize_item
from converter.pdf_parser import PdfExtractionError, detect_profile, extract_text, parse_text
from converter.reconciliation import STATUS_OK, reconcile
from converter.shopify_builder import build_dataframe

AUTO = "🔍 Détection automatique"
PREVIEW_COLUMNS = [
    "Title", "URL handle", "Vendor", "Type", "Tags", "SKU", "Barcode",
    "Option1 name", "Option1 value", "Option2 name", "Option2 value",
    "Price", "Cost per item", "Inventory quantity",
]

st.set_page_config(page_title="Facture fournisseur → Shopify", page_icon="📦", layout="wide")


# ----------------------------- Helpers -----------------------------
@st.cache_data(show_spinner=False)
def _read_text(file_bytes: bytes) -> str:
    return extract_text(io.BytesIO(file_bytes))


def _parse_document(file_bytes: bytes, choice: str) -> tuple[str, list[LineItem]]:
    """Retourne (nom du profil utilisé, lignes). Lève PdfExtractionError avec un message lisible."""
    text = _read_text(file_bytes)
    if choice == AUTO:
        return detect_profile(text, PROFILES)
    items = parse_text(text, PROFILES[choice])
    if not items:
        raise PdfExtractionError("Aucun article détecté avec ce format. Essayez la détection automatique.")
    return choice, items


def _guess_column(columns: list[str], keywords: tuple[str, ...], fallback: int) -> int:
    for idx, col in enumerate(columns):
        if any(k in str(col).lower() for k in keywords):
            return idx
    return min(fallback, len(columns) - 1)


# ----------------------------- Barre latérale -----------------------------
with st.sidebar:
    st.header("⚙️ Paramètres")
    with st.expander("💶 Prix de vente de secours"):
        st.caption("Utilisés uniquement quand le prix public TTC est absent du document.")
        coef = st.number_input("Coefficient multiplicateur", 1.0, 10.0, 2.4, 0.1, help="Prix = coût × coefficient × (1 + TVA)")
        tva = st.number_input("Taux de TVA (%)", 0.0, 30.0, 20.0, 0.5)
    with st.expander("🧩 Template Shopify (avancé)"):
        st.caption("Par défaut, le template du dépôt est utilisé.")
        template_upload = st.file_uploader("Remplacer le template", type=["csv"])

try:
    rules = load_json("product_rules.json")
    type_mapping = load_json("product_type_mapping.json")
    collections = load_json("collections.json")
    columns = template_columns(template_upload.getvalue() if template_upload else None)
except (FileNotFoundError, ValueError) as exc:
    st.error(f"Configuration invalide : {exc}")
    st.stop()

with st.sidebar:
    st.divider()
    health = config_health(rules, type_mapping, collections)
    if health:
        st.warning(f"**{len(health)} point(s) de configuration à compléter**\n\n" + "\n".join(f"- {h}" for h in health))
    else:
        st.success("Configuration complète ✅")
    if st.button("🛠️ Gérer la configuration", width="stretch", help="Marques, couleurs, types, mapping, collections"):
        config_dialog()
    with st.expander("❓ Aide"):
        st.markdown(
            "1. **Déposez** la facture ou le BL (PDF texte).\n"
            "2. **Vérifiez** les lignes extraites ; corrigez dans le tableau si besoin.\n"
            "3. **Téléchargez** le CSV et importez-le dans Shopify.\n\n"
            "Les alertes ⚠️ indiquent ce qui mérite un coup d'œil (marque inconnue, total incohérent…)."
        )


# ----------------------------- Onglet 1 : conversion -----------------------------
def render_convert() -> None:
    st.subheader("Étape 1 · Déposez votre document")
    col_up, col_fmt = st.columns([3, 2])
    pdf = col_up.file_uploader("Facture / BL fournisseur (PDF)", type=["pdf"], key="invoice")
    choice = col_fmt.selectbox(
        "Format des colonnes",
        [AUTO, *PROFILES],
        help="Laissez « détection automatique » sauf si l'extraction est incorrecte.",
    )
    if not pdf:
        st.info("👆 Déposez un PDF pour commencer. Le fichier reste sur cette session et n'est pas conservé.")
        return

    try:
        with st.spinner("Analyse du PDF…"):
            profile_name, items = _parse_document(pdf.getvalue(), choice)
    except PdfExtractionError as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # noqa: BLE001 - dernier filet de sécurité UI
        st.error(f"Erreur inattendue pendant l'analyse : {exc}")
        return

    st.subheader("Étape 2 · Vérifiez les lignes extraites")
    st.caption(
        f"Format utilisé : **{profile_name}** · Modifiez une cellule en double-cliquant, "
        "ajoutez une ligne avec ➕ en bas, supprimez en cochant la ligne puis 🗑️."
    )
    edited = st.data_editor(
        items_to_df(items),
        num_rows="dynamic",
        width="stretch",
        key=f"editor-{pdf.name}-{pdf.size}-{profile_name}",
        disabled=["Alertes"],
        column_config={
            "SKU": st.column_config.TextColumn(required=True),
            "Description": st.column_config.TextColumn(width="large"),
            "Qté": st.column_config.NumberColumn(min_value=0, step=1, format="%d"),
            "PU HT (brut)": st.column_config.NumberColumn(format="%.2f €"),
            "PU Net": st.column_config.NumberColumn(format="%.2f €"),
            "Total": st.column_config.NumberColumn(format="%.2f €"),
            "Prix public TTC": st.column_config.NumberColumn(format="%.2f €"),
            "Alertes": st.column_config.TextColumn(help="Détectées à l'extraction (non modifiables)"),
        },
    )

    try:
        products = [normalize_item(i, rules, type_mapping, collections, coef, tva) for i in df_to_items(edited)]
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
        po_choice = st.selectbox("Format du PO", [AUTO, *PROFILES], key="po_fmt")
        bl_choice = st.selectbox("Format du BL (PDF)", [AUTO, *PROFILES], key="bl_fmt")

    if not (po_file and bl_file):
        st.info("👆 Déposez les deux documents pour lancer le rapprochement.")
        return

    try:
        _, po_items = _parse_document(po_file.getvalue(), po_choice)
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
            _, bl_items = _parse_document(bl_file.getvalue(), bl_choice)
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
st.title("📦 Facture fournisseur → Shopify")
st.caption("Déposez un PDF, vérifiez les lignes, téléchargez le CSV prêt à importer.")
tab_convert, tab_reconcile = st.tabs(["1️⃣ Conversion → CSV Shopify", "2️⃣ Rapprochement PO / BL"])
with tab_convert:
    render_convert()
with tab_reconcile:
    render_reconcile()
