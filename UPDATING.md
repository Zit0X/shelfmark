# Fork : fournisseurs français (Booknode, Babelio) et mises à jour upstream

Ce fork de [calibrain/shelfmark](https://github.com/calibrain/shelfmark) ajoute
deux fournisseurs de métadonnées français. Tout le reste est identique à upstream.

## Ce que le fork ajoute

| Fichier | Rôle |
|---|---|
| `shelfmark/metadata_providers/booknode.py` | Fournisseur **Booknode** (recherche, bibliographie d'auteur, séries, fiche livre) et ses réglages |
| `shelfmark/metadata_providers/babelio.py` | Fournisseur **Babelio** (au mieux, voir plus bas) avec repli sur Booknode, et ses réglages |
| `tests/metadata/test_booknode_parse.py`, `tests/metadata/test_babelio_parse.py` | Tests |
| `shelfmark/metadata_providers/__init__.py` | **Seule modification d'un fichier upstream** : un bloc de 4 lignes qui importe les deux modules |
| `UPDATING.md` | Ce fichier |

Le bloc ajouté dans `__init__.py` est placé **en tête** de la liste des imports de
fournisseurs, juste après le commentaire `# These must be imported AFTER ...`.
Upstream ajoute ses nouveaux fournisseurs **en fin** de liste, donc les deux
modifications ne se touchent pas et Git les fusionne sans conflit.

Pas d'autre fichier upstream modifié : ni frontend (la liste des fournisseurs est
dynamique), ni `docs/environment-variables.md`, ni l'onboarding.

### Activer les fournisseurs

Réglages → *Metadata providers* → **Booknode** et/ou **Babelio** → cocher *Enable*.
Puis choisir le fournisseur dans Réglages → *General* → *Metadata provider*.
Variables d'environnement équivalentes : `BOOKNODE_ENABLED`, `BABELIO_ENABLED`,
`BABELIO_BOOKNODE_FALLBACK` (vrai par défaut).

### Limites connues

- **Booknode** : la recherche rapide renvoie au plus 5 livres pour une recherche par
  titre (limite du site). Une recherche par auteur lit toute sa bibliographie, séries
  dépliées tome par tome. Booknode n'indexe pas les ISBN.
- **Babelio** sert un captcha anti-bot (HTTP 403) aux clients non-navigateurs. Le
  fournisseur ne le contourne pas : il le détecte, ne réinterroge pas Babelio pendant
  10 minutes et, si *Fall back to Booknode* est coché, répond avec les résultats
  Booknode (ces résultats portent le fournisseur `booknode`).

### Depuis l'ancienne branche `feature/change_search_engine`

L'ancien fournisseur `babelio` interrogeait en réalité Booknode. Une configuration
existante (`BABELIO_ENABLED`, fournisseur `babelio`) continue de fonctionner grâce au
repli Booknode, et les anciens identifiants `bn:<slug>` sont toujours résolus. Il est
plus direct d'activer **Booknode** et de le choisir comme fournisseur.

## Remotes (une seule fois)

```bash
git remote -v
# origin    git@github.com:Zit0X/shelfmark.git          <- ton fork
# upstream  https://github.com/calibrain/shelfmark.git  <- dépôt officiel

# si upstream manque :
git remote add upstream https://github.com/calibrain/shelfmark.git

# mémorise la résolution des conflits pour ne pas la refaire à chaque rebase
git config rerere.enabled true
```

## Principe

- `main` = **copie exacte** de `upstream/main`. On n'y commit jamais.
- `feat/french-providers` = `main` + les commits du fork, rejoués par-dessus.
- On déploie / build depuis `feat/french-providers`.

Garder les commits du fork peu nombreux (idéalement un seul) rend chaque rebase trivial.

## Récupérer une mise à jour upstream (rebase, recommandé)

```bash
git fetch upstream

# 1. Avancer main sur upstream (avance rapide uniquement)
git switch main
git merge --ff-only upstream/main
git push origin main

# 2. Rejouer le fork par-dessus
git switch feat/french-providers
git rebase main

# 3. Vérifier
uv run pytest tests/metadata/test_booknode_parse.py tests/metadata/test_babelio_parse.py
uv run ruff check shelfmark/metadata_providers/

# 4. Publier (le rebase réécrit l'historique de la branche)
git push --force-with-lease origin feat/french-providers
```

Si `git merge --ff-only` échoue, des commits ont été faits sur `main` par erreur :
les déplacer sur la branche du fork, puis `git reset --hard upstream/main` sur `main`.

## Alternative : merge (si d'autres personnes utilisent la branche)

Ne réécrit pas l'historique, au prix de commits de merge :

```bash
git fetch upstream
git switch feat/french-providers
git merge upstream/main
git push origin feat/french-providers
```

## En cas de conflit

Le seul endroit possible est le bloc d'imports de
`shelfmark/metadata_providers/__init__.py` (si upstream modifie le début de cette
liste). Résolution : garder **la version upstream entière** et y remettre le bloc du
fork juste après le commentaire `# These must be imported AFTER ...` :

```python
# Fork: French providers (Booknode, Babelio), see UPDATING.md
with suppress(ImportError):
    from shelfmark.metadata_providers import babelio as babelio
    from shelfmark.metadata_providers import booknode as booknode
```

Puis :

```bash
git add shelfmark/metadata_providers/__init__.py
git rebase --continue      # ou : git commit   (si merge)
```

Si upstream change l'API des fournisseurs (`MetadataProvider`, `BookMetadata`,
`register_settings`, `RateLimiter` d'`openlibrary.py`), le rebase passera mais les
tests du fork échoueront : comparer avec un fournisseur upstream récent (par exemple
`databazeknih.py`, construit sur le même principe) et adapter `booknode.py`.
