# Facture / BL fournisseur → CSV Shopify

Application Streamlit qui convertit un PDF fournisseur (facture ou bon de livraison) en CSV d'import Shopify,
et rapproche un Bon de Commande (PO) d'un Bon de Livraison (BL).

## Parcours utilisateur
1. **Déposer** le PDF (format des colonnes détecté automatiquement) → 2. **Vérifier** les lignes dans un tableau éditable (indicateurs et alertes) → 3. **Télécharger** le CSV. L'onglet *Rapprochement* compare un PO et un BL ligne par ligne.

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
  storage.py                # backends de config : JSON local / Google Sheets
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

## Gérer la configuration depuis l'interface
Bouton **🛠️ Gérer la configuration** dans la barre latérale : consultation, ajout et suppression d'éléments (marques, couleurs, types de produit, mapping Product Type, collections), validation avant sauvegarde, vue et export du JSON brut. Les fichiers `config/*.json` sont réécrits : pensez à les committer.

## Stocker la configuration dans Google Sheets (optionnel)

Par défaut la config vit dans `config/*.json`. Pour la partager en équipe et l'éditer comme un tableur :

1. **Google Cloud** : créez un projet, activez l'**API Google Sheets**, créez un **compte de service** et téléchargez sa clé JSON.
2. **Google Sheets** : créez un Sheet vide et **partagez-le en Éditeur** avec l'e-mail `client_email` du compte de service.
3. **Secrets** : copiez `.streamlit/secrets.toml.example` en `.streamlit/secrets.toml` (ou collez-le dans *Secrets* sur Streamlit Cloud) et renseignez l'URL/ID du Sheet et la clé.
   Alternative sans Streamlit secrets : variables d'environnement `CONFIG_SHEET_ID` et `GOOGLE_SERVICE_ACCOUNT_JSON` (contenu du JSON).
4. Relancez l'app. Au premier démarrage, les **onglets sont créés et remplis automatiquement** depuis `config/*.json`.

| Onglet | Colonnes | Rôle |
|---|---|---|
| `brands` | `brand` | Marques reconnues |
| `colors` | `color` | Couleurs reconnues |
| `product_types` | `keywords`, `type_key`, `kind`, `label` | Règles de détection (mots-clés séparés par des virgules ; `kind` = deck, wheel, apparel, cap, other) |
| `settings` | `key`, `value` | Réglages (`sized_numeric_kinds`) |
| `type_mapping` | `type_key`, `product_type` | Association → Product Type Shopify exact |
| `collections_allowed` | `collection` | Liste blanche |
| `collections_by_type` | `type_key`, `collection` | Type → collection |

Vous pouvez éditer **dans le Sheet** (nouvelles lignes, suppression, modification ; l'ordre des colonnes est libre, ne renommez pas les en-têtes) **ou dans l'app** (🛠️ Gérer la configuration). L'app relit le Sheet toutes les ~30 s, ou immédiatement via **🔄 Recharger**.
`CONFIG_BACKEND=json` force le mode local. Si Google Sheets est configuré mais inaccessible, l'app affiche l'erreur et bascule sur les JSON locaux.

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
