"""Interface Streamlit : conversion facture/BL -> CSV Shopify + rapprochement PO/BL."""
from __future__ import annotations

import io

import pandas as pd
import streamlit as st

from converter.config import PROFILES, load_json, template_columns
from converter.models import df_to_items, items_to_df, LineItem
from converter.normalizer import normalize_item
from converter.pdf_parser import PdfExtractionError, parse_pdf
from converter.reconciliation import reconcile
from converter.shopify_builder import build_dataframe

st.set_page_config(page_title="Facture fournisseur → Shopify", page_icon="📦", layout="wide")
st.title("📦 Facture / BL fournisseur → CSV Shopify")

# ----------------------------- Barre latérale -----------------------------
st.sidebar.header("Paramètres")
coef = st.sidebar.number_input(
    "Coefficient (utilisé seulement si le prix public TTC est absent)", 1.0, 10.0, 2.4, 0.1
)
tva = st.sidebar.number_input("Taux de TVA (%)", 0.0, 30.0, 20.0, 0.5)
template_upload = st.sidebar.file_uploader("Template Shopify (optionnel)", type=["csv"])

try:
    rules = load_json("product_rules.json")
    type_mapping = load_json("product_type_mapping.json")
    collections = load_json("collections.json")
    columns = template_columns(template_upload.getvalue() if template_upload else None)
except (FileNotFoundError, ValueError) as exc:
    st.error(f"Configuration invalide : {exc}")
    st.stop()


@st.cache_data(show_spinner=False)
def _parse(file_bytes: bytes, profile: str) -> list[LineItem]:
    return parse_pdf(io.BytesIO(file_bytes), PROFILES[profile])


tab_convert, tab_reconcile = st.tabs(["1️⃣ Conversion → CSV Shopify", "2️⃣ Rapprochement PO / BL"])

# ----------------------------- Onglet 1 -----------------------------
with tab_convert:
    profile = st.selectbox("Format des colonnes numériques du document", list(PROFILES), index=1)
    pdf = st.file_uploader("Facture / BL fournisseur (PDF)", type=["pdf"], key="invoice")

    if pdf:
        try:
            with st.spinner("Analyse du PDF…"):
                items = _parse(pdf.getvalue(), profile)
        except PdfExtractionError as exc:
            st.error(str(exc))
            st.stop()
        except Exception as exc:  # noqa: BLE001 - dernier filet de sécurité UI
            st.error(f"Erreur inattendue pendant l'analyse : {exc}")
            st.stop()

        st.success(f"{len(items)} lignes détectées. Corrigez-les si besoin avant génération.")
        edited = st.data_editor(items_to_df(items), num_rows="dynamic", use_container_width=True, key="editor")

        try:
            products = [
                normalize_item(i, rules, type_mapping, collections, coef, tva) for i in df_to_items(edited)
            ]
            df = build_dataframe(products, columns)
        except (KeyError, ValueError) as exc:
            st.error(f"Impossible de générer le CSV : {exc}")
            st.stop()

        issues = [f"**{p.sku}** : {w}" for p in products for w in p.warnings]
        if issues:
            with st.expander(f"⚠️ {len(issues)} alerte(s) à vérifier", expanded=True):
                st.markdown("\n".join(f"- {m}" for m in issues))

        st.subheader("Aperçu du CSV Shopify")
        st.dataframe(df, use_container_width=True)
        st.download_button(
            "📥 Télécharger le CSV Shopify",
            data=df.to_csv(index=False).encode("utf-8"),
            file_name="shopify_import_produits.csv",
            mime="text/csv",
        )

# ----------------------------- Onglet 2 -----------------------------
with tab_reconcile:
    st.markdown("Compare les quantités du **Bon de Livraison** à l'ordre du **Bon de Commande**.")
    c1, c2 = st.columns(2)
    po_profile = c1.selectbox("Profil PO", list(PROFILES), index=3)
    po_file = c1.file_uploader("Bon de commande (PDF)", type=["pdf"], key="po")
    bl_profile = c2.selectbox("Profil BL (PDF)", list(PROFILES), index=1)
    bl_file = c2.file_uploader("Bon de livraison (PDF ou CSV)", type=["pdf", "csv"], key="bl")

    if po_file and bl_file:
        try:
            po_items = _parse(po_file.getvalue(), po_profile)
            if bl_file.name.lower().endswith(".csv"):
                bl_df = pd.read_csv(bl_file, dtype=str)
                sku_col = st.selectbox("Colonne SKU du CSV", bl_df.columns)
                qty_col = st.selectbox("Colonne quantité du CSV", bl_df.columns, index=min(1, len(bl_df.columns) - 1))
                bl_items = [
                    LineItem(sku=str(r[sku_col]).strip(), qty=int(float(r[qty_col] or 0)))
                    for _, r in bl_df.iterrows()
                ]
            else:
                bl_items = _parse(bl_file.getvalue(), bl_profile)
        except PdfExtractionError as exc:
            st.error(str(exc))
            st.stop()
        except (ValueError, KeyError) as exc:
            st.error(f"Fichier BL invalide : {exc}")
            st.stop()

        result = reconcile(po_items, bl_items)
        (st.success if result.ok else st.warning)(result.header)
        if not result.ok:
            st.markdown("\n".join(f"- {d}" for d in result.discrepancies))
        st.subheader("Quantités (ordre exact du PO)")
        st.code(result.quantities_text(), language=None)
        st.caption(f"Total livré : {sum(q for q in result.quantities if q is not None)}")
