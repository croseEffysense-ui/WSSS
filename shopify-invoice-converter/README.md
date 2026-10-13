# Facture / BL fournisseur → CSV Shopify

Application Streamlit qui convertit un PDF fournisseur (facture ou bon de livraison) en CSV d'import Shopify,
et rapproche un Bon de Commande (PO) d'un Bon de Livraison (BL).

## Lancer
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
pytest            # tests
```

## Structure
```
app.py                      # interface Streamlit (2 onglets)
converter/
  pdf_parser.py             # PDF -> lignes (SKU, description, qté, prix, EAN)
  normalizer.py             # règles métier : marque, type, titre, options, prix, poids
  shopify_builder.py        # lignes -> CSV (colonnes du template, variantes, consolidation)
  reconciliation.py         # PO vs BL
  config.py / models.py
config/                     # À COMPLÉTER (voir ci-dessous)
  product_rules.json        # marques, couleurs, mots-clés de types
  product_type_mapping.json # type_key -> Product Type Shopify exact
  collections.json          # liste blanche des collections
data/product_template.csv   # template Shopify (ordre des colonnes)
tests/
```

## À compléter avant la première vraie utilisation
- `product_type_mapping.json` : seule la correspondance `wallet → Porte Feuille` est renseignée ; les autres
  sont vides (une alerte s'affiche tant qu'elles le sont). Ajoutez votre table officielle.
- `collections.json` : liste blanche vide. Le template CSV n'a pas de colonne « Collection » : les collections
  autorisées sont ajoutées dans `Tags`.
- `product_rules.json` : ajoutez vos marques / couleurs / types réels.

## Bonnes pratiques GitHub
- Branche `main` protégée, travail via branches `feature/...` + Pull Requests.
- Ne jamais committer de vraies factures (`*.pdf` est dans `.gitignore`) ; utiliser des PDF anonymisés pour les tests.
- Ajouter un workflow GitHub Actions lançant `pytest` à chaque push.
- Versionner les fichiers `config/*.json` : ce sont vos règles métier, elles doivent être relues en PR.
- Épingler les versions dans `requirements.txt` une fois le projet stabilisé.
