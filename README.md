# LinkedIn Scraper — Guide complet & clair

Ce README explique **comment utiliser** la pipeline, **quoi configurer**, **comment relancer** chaque étape, et **comment mettre à jour les sélecteurs** quand l’HTML de Google/LinkedIn change.  
Tout est aligné avec **ta commande** :

```powershell
python -m linkedin_scrapper.main --config linkedin_scrapper\config\params_run.json
```

---

## Sommaire

1. [Prérequis](#prérequis)  
2. [Arborescence attendue](#arborescence-attendue)  
3. [Configuration (`config/params_run.json`)](#configuration-configparams_runjson)  
4. [Lancer la pipeline (tout-en-un)](#lancer-la-pipeline-tout-en-un)  
5. [Étapes en détail](#étapes-en-détail)  
   - [Étape 1 — Recherche d’URL de profil](#étape-1--recherche-durl-de-profil)  
   - [Étape 2 — Scrape des profils](#étape-2--scrape-des-profils)  
   - [Étape 3 — Reporting (optionnel)](#étape-3--reporting-optionnel)  
6. [Maintenance : mettre à jour les sélecteurs (Google/LinkedIn)](#maintenance--mettre-à-jour-les-sélecteurs-googlelinkedin)  
7. [Dépannage rapide](#dépannage-rapide)  
8. [FAQ](#faq)

---

## Prérequis

- **Python 3.10+**
- **Playwright + navigateurs** :
  ```bash
  python -m pip install -r requirements.txt
  python -m playwright install
  ```
- Un CSV d’entrée **UTF-8** avec les colonnes : `nom,keywords`.  
  Exemple : `data/inputs/noms.csv`

---

## Arborescence attendue

```
.
├─ main.py
├─ config/
│  ├─ params_run.json
│  └─ selectors.json
├─ data/
│  ├─ inputs/
│  │  └─ noms.csv
│  └─ outputs/
├─ step1_urls_finder/
│  ├─ google_searcher.py
│  ├─ linkedin_searcher.py        
│  └─ main_urls_finder.py         
├─ step2_profile_scraper/
│  ├─ scrape_profiles.py
│  └─ linkedin_sections_extractor.py
└─ utils_scripts/
   ├─ context_factory.py
   ├─ linkedin_login.py
   ├─ guards.py
   ├─ selectors_registry.py
   └─ utils_async.py
```


---

## Configuration (`config/params_run.json`)

Exemple **réel** :

```json
{
  "names_csv": "linkedin_scrapper/data/inputs/noms.csv",
  "step1_out": null,
  "step2_in": null,
  "skip_step1": false,
  "skip_step2": false,
  "skip_step3": false,
  "save_intermediate": true,
  "tag": "test_dev",
  "limit_step2": 100,
  "save_every": 10,
  "ref_date": "2022-02-02"
}
```

### Signification

- `names_csv` : chemin vers le CSV d’entrée (`nom,keywords`).
- `step1_out` : (optionnel) CSV **déjà produit** par l’étape 1 (permet de sauter Step1).
- `step2_in`  : (optionnel) JSONL/CSV **déjà produit** par l’étape 2 (permet de sauter Step2).
- `skip_step1` / `skip_step2` / `skip_step3` : **sauter** des étapes.
- `save_intermediate` : `true` → **écrit** le CSV de Step1 dans `data/outputs/...`.
- `tag` : suffixe lisible pour nommer les sorties (`client_fr`, etc.).
- `limit_step2` : limite le nombre de profils scrappés (debug/POC).
- `save_every` : flush JSONL toutes les N lignes (résilience).
- `ref_date` : (Step3) date de référence pour calculs (format souple, `YYYY-MM-DD` conseillé).

---

## Lancer la pipeline (tout-en-un)

### Basic (recommandé)

```powershell
python -m linkedin_scrapper.main --config linkedin_scrapper\config\params_run.json
```

### Rejouer **uniquement** le scrape (Step2) en réutilisant Step1

Dans `params_run.json` :

```json
"skip_step1": true,
"step1_out": "C:\\\\...\\\\data\\\\outputs\\\\pipeline_search_results_<TAG>_<DATE>.csv",
"step2_in": null
```

Puis :

```powershell
python .\main.py --config .\config\params_run.json
```

### Passer directement au reporting (Step3)

```json
"skip_step1": true,
"skip_step2": true,
"step2_in": "C:\\\\...\\\\data\\\\outputs\\\\step2_profiles\\\\profiles_<TAG>_<DATE>.jsonl"
```

---

## Étapes en détail

### Étape 1 — Recherche d’URL de profil

Orchestré par `step1_urls_finder/main_urls_finder.py`.  
Ordre d’exécution et **priorité** de sélection d’URL :

1) **Google mode 1** → `site:linkedin.com/in {nom} datascientest` → `URL_google_m1`  
2) **Google mode 2** → `Linkedin {nom} {keywords}` → `URL_google_m2`  
3) **LinkedIn interne – mode 3** → `query = "nom datascientest"` → `URL_linkedin_m3`  
4) **LinkedIn interne – mode 4** → `query = "nom keywords"` → `URL_linkedin_m4`

Ensuite on **choisit** la première URL non vide selon :  
`google_m1 > google_m2 > linkedin_m3 > linkedin_m4` → colonnes finales `URL` + `status`.

**Sortie Step1** (si `save_intermediate=true`) :
```
data/outputs/pipeline_search_results_<TAG>_<DATE>.csv
# colonnes : nom, keywords, URL, status
```


---

### Étape 2 — Scrape des profils

```bash
python -m step2_profile_scraper.scrape_profiles \
  --csv data/outputs/pipeline_search_results_<TAG>_<DATE>.csv \
  --tag client_fr \
  --limit 50 \
  --save-every 10
```

- Simule des actions humaines (mouvements souris, scroll, pauses).
- **Anti-détection** : rate-limit, backoff exponentiel, **circuit-breaker** (pause longue si score réseau élevé), détection **CAPTCHA/checkpoint**.
- **Sélecteurs externalisés** dans `config/selectors.json` (LinkedIn & Google).

**Sorties Step2** :
- `data/outputs/step2_profiles/profiles_<TAG>_<DATE>.jsonl` (source de vérité)  
- `data/outputs/step2_profiles/profiles_<TAG>_<DATE>.csv` (miroir pratique)

---

### Étape 3 — Reporting (optionnel)

- Lit le JSONL/CSV de Step2, synthétise par `status`, exporte un CSV final.  
- Peut produire un petit **résumé JSON** : `data/outputs/step2_profiles/logs/summary_*.json`.

---

## Maintenance : mettre à jour les sélecteurs (Google/LinkedIn)

Le scraper **lit ses sélecteurs** depuis `config/selectors.json` via `utils_scripts/selectors_registry.py`.  
Quand l’UI change, **modifiez le JSON**, pas le Python.

### Où sont utilisés ces sélecteurs ?

- **LinkedIn**
  - `linkedin.profile.sections.*` : localiser les conteneurs (éducation / expériences)
  - `linkedin.profile.fields.*`   : extraire les champs (titre, sous-titre, meta, description)
  - `linkedin.search.*`           : barre de recherche + lien du 1er profil en résultats
- **Google**
  - `google.search.input`         : champ de recherche
  - `google.search.results_links` : premier lien de résultat (ou son `<h3>`)

### Structure (résumé)

```json
{
  "linkedin": {
    "locale_default": "fr",
    "locales": {
      "fr": {
        "profile": {
          "sections": {
            "education": { "containers": [...], "items": [...] },
            "experience": { "containers": [...], "items": [...] }
          },
          "fields": {
            "title": [...],
            "subtitle": [...],
            "meta": [...],
            "description": [...]
          },
          "buttons": {
            "see_all_education": [...],
            "see_all_experience": [...]
          }
        },
        "search": {
          "input": [...],
          "result_profile_link": [...]
        }
      },
      "en": { "..." : "mêmes clés avec sélecteurs adaptés" }
    }
  },
  "google": {
    "search": {
      "input": [...],
      "results_links": [...],
      "next_page": [...]
    }
  }
}
```

> Le code prend **le premier sélecteur visible**.  
> Ajoutez le **nouveau sélecteur en tête** de liste, **sans supprimer** l’ancien (rétro-compatibilité).

### Méthode rapide pour trouver un bon sélecteur

1. Ouvrir **DevTools (F12)** → Inspecter l’élément cible.  
2. Repérer une **classe stable** (`pvs-entity`, `reusable-search__result-container`, etc.).  
3. Créer des sélecteurs **spécifiques mais robustes** :  
   - ✅ `section[data-view-name*='experience']`, `li.pvs-list__paged-list-item`, `a.app-aware-link[href^='https://www.linkedin.com/in/']`  
   - ❌ IDs éphémères / classes hashées  
4. Ajouter **en tête** dans `selectors.json`.  
5. **Tester** avec le script ci-dessous.

### Script de test (rapide)

Créez `scripts/test_selectors.py` :

```python
# scripts/test_selectors.py
import asyncio
from utils_scripts.context_factory import launch_browser, new_context
from utils_scripts.selectors_registry import sel_list
from config import settings

URL = "https://www.google.com"  # ou "https://www.linkedin.com/feed/"
HEADLESS = False

async def main():
    pw, browser = await launch_browser(headless=HEADLESS)
    try:
        ctx = await new_context(browser, storage_state_path=None)
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded", timeout=settings.navigation_timeout_ms)

        # Exemple: tester l'input Google
        selectors = sel_list("google", "search", "input")
        print("Testing selectors:", selectors)
        ok = False
        for css in selectors:
            try:
                loc = page.locator(css).first
                await loc.wait_for(state="visible", timeout=2500)
                print("[OK]", css)
                ok = True
                break
            except Exception:
                print("[..] not visible:", css)
        if not ok:
            print("[FAIL] Aucun sélecteur n'a matché.")
    finally:
        await browser.close()
        await pw.stop()

if __name__ == "__main__":
    asyncio.run(main())
```

Exécuter :
```bash
python -m scripts.test_selectors
```

---

## Dépannage rapide

- **Connexion LinkedIn** : au premier run headful, connectez-vous manuellement ; la session est stockée (storage state).
- **CAPTCHA / Checkpoint** : la pipeline applique un backoff et un “reset doux” vers `/feed`.  
  Si les challenges s’enchaînent, le **circuit-breaker** met le compte en **pause longue** (log explicite).
- **Sélecteurs cassés** : mettez à jour `config/selectors.json` (voir section maintenance), puis testez avec `scripts/test_selectors.py`.
- **Reprise de run** :
  - Step1 déjà fait → mettez `step1_out` et `skip_step1=true`
  - Step2 déjà fait → mettez `step2_in` et `skip_step2=true`
- **Sauvegardes** :
  - Step1 → CSV : `data/outputs/pipeline_search_results_*.csv`
  - Step2 → JSONL + CSV miroir : `data/outputs/step2_profiles/*`

---

## FAQ

**Q. Pourquoi Google avant LinkedIn ?**  
A. C’est moins intrusif et souvent plus stable. LinkedIn interne (modes 3/4) sert de complément.

**Q. Où changer “datascientest” ?**  
A. Dans la construction des requêtes :
- **Google** mode 1 : `site:linkedin.com/in {nom} datascientest`  
- **LinkedIn** mode 3 : `query = "{nom} datascientest"`  
Vous pouvez le retirer ou le remplacer selon votre cas métier.

**Q. Je veux forcer un seul mode.**  
A. Vous pouvez appeler directement :
- `step1_urls_finder.google_searcher.main(query_mode=1|2, csv_file=...)`
- `step1_urls_finder.linkedin_searcher.entry_batch(..., query_mode=3|4)`  
ou ajuster la priorité dans `step1_urls_finder/main_urls_finder.py` si besoin.

**Q. Où sont les options anti-détection ?**  
A. Dans `utils_scripts/guards.py` (rate-limit, backoff, breaker) + les “behaviors” (mouvements souris, scroll) et pacing dans les modules Step1/Step2.

---
