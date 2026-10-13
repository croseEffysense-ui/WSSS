import io
import re
import pandas as pd
import pypdf
import streamlit as st

# Configuration de la page Streamlit
st.set_page_config(
    page_title="Convertisseur Facture Fournisseur -> Shopify", layout="wide"
)

st.title("📦 Convertisseur de Factures Fournisseur pour Shopify")
st.markdown(
    "Glissez votre facture PDF fournisseur ci-dessous pour générer le fichier CSV prêt à être importé dans Shopify."
)

# Facteur de marge par défaut (ex: x2.5 par rapport au prix d'achat HT)
marge_coef = st.sidebar.number_input(
    "Coefficient multiplicateur (Prix de vente = Prix d'achat x Coef)",
    min_value=1.0,
    max_value=10.0,
    value=2.4,
    step=0.1,
)
tva_taux = st.sidebar.number_input(
    "Taux de TVA (%)", min_value=0.0, max_value=30.0, value=20.0, step=0.5
)


def extract_invoice_data(pdf_file):
  reader = pypdf.PdfReader(pdf_file)
  full_text = ""
  for page in reader.pages:
    full_text += page.extract_text() + "\n"

  lines = full_text.split("\n")
  items = []

  # Expression régulière pour détecter un SKU type (ex: SCA-TEE-12915, TRI-PHE-0068, 8BL-PHE-0017)
  sku_pattern = re.compile(
        r"^(?:[A-Z0-9]{2,3}-[A-Z]{3}-\d+|[A-Z0-9]{2,3}-[A-Z]{3}\d*-?|[A-Z0-9-]+)$"
    )

  i = 0
  while i < len(lines):
    line = lines[i].strip()

    # Si la ligne ressemble à un SKU potentiel
    if (
        len(line) >= 5
        and "-" in line
        and not "Page" in line
        and not "Order" in line
    ):
      sku = line.replace(" ", "")
      description_lines = []
      i += 1

      # On récupère la description multiligne jusqu'à trouver les chiffres (Qty, Price, Total)
      while i < len(lines):
        next_line = lines[i].strip()
        # Pattern de fin de ligne d'article : contient des chiffres séparés par des espaces (ex: "2 10.87 21.74")
        match_end = re.search(r"^(\d+)\s+([\d.]+)\s+([\d.]+)$", next_line)
        if match_end:
          qty = int(match_end.group(1))
          price_ht = float(match_end.group(2))
          break
        elif (
            re.search(r"^\d+$", next_line)
            and i + 2 < len(lines)
            and "." in lines[i + 1]
        ):
          # Cas où la quantité est seule sur sa ligne
          qty = int(next_line)
          price_ht = float(lines[i + 1].strip())
          i += 2
          break
        else:
          if next_line and not "Page" in next_line:
            description_lines.append(next_line)
          i + 1  # Correction logique d'itération
        i += 1

      full_desc = " ".join(description_lines)
      items.append(
          {
              "SKU": sku,
              "Description": full_desc,
              "Qty": locals().get("qty", 1),
              "PriceHT": locals().get("price_ht", 0.0),
          }
      )
    i += 1

  return items


uploaded_file = st.file_uploader(
    "Télécharger la facture fournisseur (PDF)", type=["pdf"]
)

if uploaded_file is not None:
  with st.spinner("Analyse du document PDF en cours..."):
    items = extract_invoice_data(uploaded_file)

  if items:
    st.success(
        f"Succès ! {len(items)} produits détectés dans la facture fournisseur."
    )

    # Transformation pour correspondre au format Shopify
    shopify_rows = []
    for item in items:
      sku = item["SKU"]
      title = item["Description"]
      cost_price = item["PriceHT"]
      sell_price = round(cost_price * marge_coef, 2)
      qty = item["Qty"]

      # Extraction basique d'options (Taille / Couleur si présentes)
      size = "Unique"
      for s in ["XS", "S", "M", "L", "XL", "XXL", "30", "32", "34", "36", "41", "42", "43", "44", "S/M", "L/XL"]:
        if f" {s} " in title or title.endswith(f" {s}"):
          size = s
          break

      shopify_rows.append({
          "Title": title[:50],  # Titre tronqué propre
          "URL handle": title.lower().replace(" ", "-")[:50],
          "Description": title,
          "Vendor": "Fournisseur Externe",
          "Type": "Skatewear / Accessoire",
          "Published on online store": "True",
          "Status": "Active",
          "SKU": sku,
          "Option1 name": "Size",
          "Option1 value": size,
          "Price": sell_price,
          "Cost per item": cost_price,
          "Charge tax": "True",
          "Inventory tracker": "shopify",
          "Inventory quantity": qty,
          "Requires shipping": "True",
          "Weight value (grams)": 300,
          "Weight unit for display": "g",
      })

    df_shopify = pd.DataFrame(shopify_rows)

    # Affichage d'un aperçu
    st.dataframe(df_shopify)

    # Bouton de téléchargement du CSV
    csv = df_shopify.to_csv(index=False).encode("utf-8")
    st.download_button(
        label="📥 Télécharger le fichier CSV pour Shopify",
        data=csv,
        file_name="shopify_import_produits.csv",
        mime="text/csv",
    )
  else:
    st.warning(
        "Aucun article n'a pu être extrait automatiquement. Vérifiez le format du PDF."
    )
