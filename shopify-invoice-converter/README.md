# Facture / BL fournisseur → CSV Shopify

Application Streamlit qui convertit un PDF fournisseur (facture ou bon de livraison) en CSV d'import Shopify,
et rapproche un Bon de Commande (PO) d'un Bon de Livraison (BL).

## Parcours utilisateur
1. **Déposer** le PDF et choisir le **format fournisseur** (ou laisser la détection automatique) → 2. **Vérifier** les lignes dans un tableau éditable (indicateurs et alertes) → 3. **Télécharger** le CSV. L'onglet *Rapprochement* compare un PO et un BL ligne par ligne.

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
  logs.py / diagnostics.py  # journalisation (secrets masqués) / diagnostic pas à pas
  pdf_parser.py             # PDF -> lignes (SKU, description, qté, prix, EAN)
  normalizer.py             # règles métier : marque, type, titre, options, prix, poids
  shopify_builder.py        # lignes -> CSV (colonnes du template, variantes, consolidation)
  reconciliation.py         # PO vs BL
  formats.py                # formats de factures par fournisseur (colonnes, validation)
  config.py / models.py
config/                     # À COMPLÉTER (voir ci-dessous)
  product_rules.json        # marques, couleurs, mots-clés de types
  product_type_mapping.json # type_key -> Product Type Shopify exact
  collections.json          # liste blanche des collections
  supplier_formats.json     # formats de factures / BL par fournisseur
data/product_template.csv   # template Shopify (ordre des colonnes)
tests/
```

## Formats de factures par fournisseur
Chaque fournisseur a sa façon de présenter les colonnes chiffrées d'une ligne article. Un **format** décrit ces colonnes, **de gauche à droite** (jusqu'à 5 : Qté, PU HT (brut), PU Net, Total, Prix public TTC ; la Qté est obligatoire).

**🛠️ Gérer la configuration → Formats fournisseurs** : ajoutez une ligne par fournisseur (nom + *Colonne 1…5*), modifiez ou supprimez-en, puis **💾 Enregistrer**. Les formats enregistrés alimentent la liste **« Format fournisseur »** (conversion et rapprochement PO/BL) et la détection automatique. Les 4 formats fournis sont des exemples : renommez-les au nom de vos fournisseurs.

Dans Google Sheets : onglet `supplier_formats` (colonnes `name`, `columns`, `notes`) ; `columns` est une liste séparée par des virgules, p. ex. `Qté, PU Net, Total` (variantes acceptées : `qty`, `quantité`, `pu ht`, `prix public`…).

## Gérer la configuration depuis l'interface
Bouton **🛠️ Gérer la configuration** dans la barre latérale : consultation, ajout et suppression d'éléments (formats fournisseurs, marques, couleurs, types de produit, mapping Product Type, collections), validation avant sauvegarde, vue et export du JSON brut. Les fichiers `config/*.json` sont réécrits : pensez à les committer.

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
| `supplier_formats` | `name`, `columns`, `notes` | Formats de factures par fournisseur |

Vous pouvez éditer **dans le Sheet** (nouvelles lignes, suppression, modification ; l'ordre des colonnes est libre, ne renommez pas les en-têtes) **ou dans l'app** (🛠️ Gérer la configuration). L'app relit le Sheet toutes les ~30 s, ou immédiatement via **🔄 Recharger**.
`CONFIG_BACKEND=json` force le mode local. Si Google Sheets est configuré mais inaccessible, l'app affiche l'erreur et bascule sur les JSON locaux.

## Dépannage de la connexion Google Sheets

Cliquez sur **🩺 Diagnostic & journal** (barre latérale) puis **▶️ Lancer le diagnostic** : l'application teste chaque maillon dans l'ordre et désigne **le premier blocage** avec une piste de résolution.

| Étape testée | Panne typique détectée |
|---|---|
| Fichiers `secrets.toml` | fichier absent, mal nommé (`secrets.toml.txt`), **TOML invalide** (ligne/colonne indiquées) |
| Choix du backend / champs du compte de service | section manquante, `type` ≠ `service_account`, clé incomplète |
| Clé privée lisible | `private_key` corrompue ou `\n` mal collés (corrigés automatiquement si littéraux) |
| Réseau / horloge | pare-feu, proxy, VPN, horloge décalée |
| Jeton OAuth | clé révoquée ou supprimée, horloge |
| Accès au Sheet | **API Google Sheets non activée** (lien direct fourni), Sheet non partagé avec le compte de service, ID/URL faux, fichier `.xlsx` non converti |
| Onglets / en-têtes | onglet manquant, colonne renommée |
| Droit d'écriture (option) | Sheet partagé en « Lecteur » au lieu d'« Éditeur » |

L'onglet **Journal d'exécution** affiche tous les logs (niveau DEBUG compris, requêtes HTTP incluses). Ils sont aussi écrits dans la console et dans `logs/converter.log`. **Les secrets (clé privée, jetons) sont masqués automatiquement.** Les deux boutons « Télécharger » produisent un rapport/journal que vous pouvez partager sans risque. `LOG_LEVEL=INFO` réduit le bruit de la console.
Après correction, cliquez sur **🔁 Réessayer la connexion**.

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
