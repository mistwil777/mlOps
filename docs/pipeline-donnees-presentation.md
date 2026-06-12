# Pipeline de Données — MLOps IA Souveraine
## De la collecte web au fine-tuning local : comment nous garantissons la qualité des données

**Projet de référence :** Migrator — Migration Java→Python  
**Équipe :** Direction Technology & Innovation — Capgemini  
**Date :** Juin 2026  
**Niveau :** Document de référence — présentation interne

---

## Vue d'ensemble

Pour entraîner un modèle d'IA capable de migrer du code Java vers Python, nous avons besoin de milliers d'exemples de qualité irréprochable. Ce document décrit, étape par étape, comment nous collectons ces données sur le web, comment nous les filtrons, les organisons et les vérifions — jusqu'à ce qu'elles soient prêtes pour l'entraînement.

**Principe fondamental — GIGO :** *Garbage In, Garbage Out*. Un modèle entraîné sur de mauvaises données produira de mauvais résultats. Chaque étape de ce pipeline est conçue pour écarter les données inutiles ou incorrectes.

---

> **Le pipeline est conçu pour tout type de projet**
>
> Le projet Migrator sert de cas de référence dans ce document, mais le pipeline est entièrement agnostique — le même code, les mêmes étapes, les mêmes filtres s'appliquent à n'importe quel projet client.
>
> | Type de projet | Domaine | Stratégie | Exemples |
> |---|---|---|---|
> | Migration de code | code | fuse / code_migration | Java→Python, Python 2→3, Angular 14→17 |
> | Chatbot métier | text | qa | RH Onboarding, Legal, Support client |
> | Review de qualité | code | doc_to_code | Revue de code, conformité, sécurité |
> | Classification | text | qa / generation | Sensibilité documents, triage tickets |
>
> Seul le fichier de configuration YAML change d'un projet à l'autre. Le pipeline, lui, ne change pas.

---

## Schéma général du pipeline

```
INTERNET                       TRAITEMENT                    RÉSULTAT
─────────                      ──────────                    ────────
Rosetta Code   ─┐
Documentation  ─┤              ┌─ Nettoyage ─┐
GitHub/Web     ─┤   Collecte   │  (clean_raw)│   Fusion   ┌─ Nettoyage ─┐   Fine-
Génération LLM ─┘   ─────────► │  7 filtres  ├──────────► │  (clean)    │ ► Tuning
                                └─────────────┘             └─────────────┘   Ready
```

**En chiffres (projet Migrator) :**
- Données brutes collectées : **15 250 records**
- Après filtrage qualité : **2 770 records** (taux de rejet ~82% — voulu)
- Paires d'entraînement formées : **3 600**
- Prêtes pour fine-tuning : **cible 4 860**

---

## Partie 1 — La Collecte des Données


### 1.1 Sources utilisées

Nous collectons depuis 3 types de sources, chacune couvrant un besoin différent :

#### Source A — Rosetta Code (bilingue)
**Qu'est-ce que c'est ?** Un wiki collaboratif qui implémente le même algorithme dans des centaines de langages différents. Chaque tâche (ex : "Fibonacci", "Tri à bulles") existe en Java ET en Python avec le même nom.

**Pourquoi c'est précieux ?** C'est la seule source où nous sommes *certains* que le code Java et le code Python font exactement la même chose. Ce sont les paires les plus fiables.

**Exemple concret :**

```
Tâche : "Binary Search"

Version Java :
  public static int binarySearch(int[] arr, int target) {
      int left = 0, right = arr.length - 1;
      while (left <= right) {
          int mid = (left + right) / 2;
          if (arr[mid] == target) return mid;
          ...
      }
  }

Version Python :
  def binary_search(arr, target):
      left, right = 0, len(arr) - 1
      while left <= right:
          mid = (left + right) // 2
          if arr[mid] == target: return mid
          ...
```

**Ce que le collecteur fait :** Appelle l'API MediaWiki de Rosetta Code, récupère le wikitext brut, extrait les blocs de code par langage. Normalise les noms de langues en minuscules (Java → java).

**Paramètre clé :** Code minimum 30 caractères (élimine les snippets triviaux)

**Autres cas d'usage :** Rosetta Code est spécifique à la migration de code (paires bilingues). Pour un projet RH ou Legal, cette source n'est pas utilisée — elle est simplement absente du fichier de config.

---

#### Source B — Documentation officielle et tutoriels

**Qu'est-ce que c'est ?** Crawl automatique de pages comme `docs.python.org`, `docs.oracle.com`, `baeldung.com`, `programiz.com`. Chaque page contient du texte explicatif + des blocs de code.

**Ce que le collecteur extrait :** Le texte qui précède un bloc de code (la description) + le bloc de code lui-même + le langage.

**Exemple concret :**

```
Page : docs.python.org/3/library/functions.html#filter

Description extraite :
  "Return an iterator from those elements of iterable for which 
   function returns true."

Code extrait :
  filtered = list(filter(lambda x: x > 0, [-1, 2, -3, 4]))
  # Résultat : [2, 4]

Langage : python
```

**Sites de confiance utilisés :**
- Documentation officielle : docs.python.org, docs.oracle.com, docs.spring.io, junit.org, docs.sqlalchemy.org, pydantic.dev, fastapi.tiangolo.com
- Tutoriels qualité : baeldung.com, programiz.com, geeksforgeeks.org, realpython.com, datacamp.com, developpez.com

**Paramètres clés :**
- Profondeur de crawl : 5 niveaux maximum
- Pages par site : 200 maximum
- Délai entre requêtes : 0.8 seconde (politesse réseau)
- Code minimum : 30 caractères, 2 lignes

**Autres cas d'usage :** Cette source s'applique à tous les projets. Pour le projet **RH Onboarding**, on crawlerait les pages de la convention collective publiées en ligne, les guides RH Capgemini (intranet), ou les FAQ de portails RH publics. Pour le projet **Legal Defense**, uniquement des textes réglementaires publics (DMSST, legifrance.gouv.fr) — zéro donnée interne sur internet.

---

#### Source C — Découverte autonome (AWA)
**Qu'est-ce que c'est ?** Un agent qui cherche lui-même de nouvelles sources adaptées à chaque adapter, en utilisant des mots-clés spécifiques (ex : "JPA java tutorial code example").

**Comment ça fonctionne :**
1. Recherche via SearXNG (moteur local qui agrège Google, Bing, DuckDuckGo) → si indisponible, DuckDuckGo direct → si bloqué (403), Brave Search API
2. Filtre les résultats : uniquement les domaines de confiance listés
3. Vérifie que chaque page contient réellement du code dans le langage attendu
4. Retourne une liste de nouvelles URLs à crawler

**Exemple de requête AWA :** `"jpa java tutorial code example (site:baeldung.com OR site:docs.oracle.com)"`

**Autres cas d'usage :** Pour le projet **RH Onboarding**, l'AWA chercherait `"convention collective RTT congés FAQ (site:service-public.fr OR site:legifrance.gouv.fr)"`. Pour le projet **Python 2→3 Migrator**, il chercherait `"python2 to python3 migration guide code example (site:docs.python.org OR site:realpython.com)"`. Les mots-clés sont configurés par adapter dans le YAML du projet, pas dans le code.

> **Contrainte souveraineté (Legal Defense) :** Pour les projets à données classifiées, la Source C (AWA) est désactivée dans le config. L'ingestion est exclusivement manuelle via des fichiers déposés localement dans `datalake/raw/`.

---

### 1.2 Ce que nous ne collectons pas — les formats exclus

Pour éviter les données parasites :

| Format exclu | Raison |
|---|---|
| Bash, Shell, CMD | Scripts système, pas du code applicatif |
| XML, JSON, YAML | Formats de configuration, pas du code |
| HTML, CSS | Présentation, pas de logique |
| SQL pur | Couvert par un adapter dédié |
| Code < 30 caractères | Trop trivial pour être utile |
| Pages nav/login/cookies | Boilerplate sans valeur |

---

### 1.3 Format de sortie de la collecte

Tous les collecteurs produisent un format neutre identique :

**Exemple — domaine code (projet Migrator) :**
```json
{
  "description": "Filter a list using a predicate function.",
  "code": "filtered = list(filter(lambda x: x > 0, numbers))",
  "lang": "python",
  "source_url": "https://docs.python.org/3/library/functions.html",
  "collected_at": "2026-06-09T10:00:00Z",
  "adapter_name": "java-to-python-translator"
}
```

**Exemple — domaine text (projet RH Onboarding) :**
```json
{
  "question": "Combien de jours de congés ai-je droit ?",
  "answer": "Selon l'article 12 de la convention collective Syntec, tout salarié acquiert 2,5 jours ouvrables de congés par mois travaillé, soit 30 jours ouvrables par an.",
  "domain": "text",
  "task_type": "qa",
  "source_url": "guide-rh-interne.pdf",
  "adapter_name": "hr-onboarding-qa"
}
```

**Important :** À ce stade, il n'y a PAS encore de paires d'entraînement. Ce sont des records bruts (monolingues pour le code, question/réponse pour le texte). Les paires se forment plus tard.

---

## Partie 2 — Nettoyage des Données Brutes (clean_raw)

### Pourquoi nettoyer ?

Sur les 15 250 records collectés, une grande partie est inutilisable :
- Doublons (même page indexée par plusieurs chemins)
- Code de navigation HTML ("Home | Login | Search...")
- Snippets trop courts ou syntaxiquement invalides
- Données hors-sujet par rapport à l'adapter

Cette étape conserve uniquement les données réellement utiles.

### 7 filtres successifs

#### Filtre 1 — Déduplication exacte

**Comment :** Calcul d'une empreinte numérique (hash MD5) sur `description + code`. Si deux records ont la même empreinte, le second est supprimé.

**Exemple :**
```
Record 1 : description="Sort a list", code="def bubble_sort(arr):..."
Record 2 : description="Sort a list", code="def bubble_sort(arr):..."  ← SUPPRIMÉ (identique)
```

**Impact typique :** ~15-30% des records sont des doublons exacts (même source indexée plusieurs fois)

---

#### Filtre 2 — Score de qualité heuristique

Chaque record reçoit un score de 0 à 1 basé sur des critères objectifs, sans IA :

**Pour le code :**

| Critère | Points |
|---|---|
| Code entre 50 et 1500 caractères | +0.40 |
| Présence de mots-clés structurels (def, class, public...) | +0.20 |
| Description présente (> 20 caractères) | +0.15 |
| Syntaxe Python valide (vérifiée par AST) | +0.15 |
| Au moins 3 lignes de code | +0.10 |
| Syntaxe Python INVALIDE | −0.10 |
| Ressemble à un titre de page | −0.15 |
| **Seuil d'acceptation** | **≥ 0.55** |

**Pour Java spécifiquement :**
- 2+ mots-clés structurels (class, interface, public, void...) → +0.15
- Accolades équilibrées `{ }` → +0.05

**Exemple de rejet :**
```
Code : "Home > Java > Collections"      → Score 0.0 → REJETÉ (navigation HTML)
Code : "public class Sort { }"          → Score 0.65 → CONSERVÉ
Code : "def sort(arr): return sorted"   → Score 0.80 → CONSERVÉ
```

**Sur un projet RH (domaine text) :** Les critères sont adaptés — pas de vérification AST ni de mots-clés structurels de code. À la place : entropie du texte (≥ 3.5 bits/char), longueur de la réponse (≥ 50 mots), absence de boilerplate RH vide ("Merci de contacter les RH"). Le seuil d'acceptation reste 0.55.

---

#### Filtre 3 — Déduplication sémantique

Deux records peuvent avoir du code identique mais avec des descriptions différentes. Le filtre exact ne les détecte pas. Ce filtre compare le *sens* des descriptions.

**Comment :** Conversion de chaque description en vecteur numérique (embeddings via AWS Bedrock Titan), puis calcul de similarité cosinus. Si deux records ont une similarité ≥ 0.92, c'est un quasi-doublon.

**Exemple :**
```
Record A : "Write a Python function for Fibonacci sequence"
           code: def fib(n): if n <= 1: return n...

Record B : "Implement the Fibonacci sequence in Python"
           code: def fib(n): if n <= 1: return n...   ← QUASI-DOUBLON (sim = 0.96)
```
Record B supprimé — même code, description quasi-identique.

**Seuil :** 0.92 de similarité (au-delà = quasi-doublon)

---

#### Filtre 4 — Pertinence par rapport à l'adapter

Chaque record est comparé sémantiquement à la description de l'adapter pour lequel il est destiné. Un record sur la gestion mémoire C++ n'a pas sa place dans l'adapter JUnit→pytest.

**Comment :** Embedding de la description de l'adapter + calcul de similarité cosinus avec chaque record.

**Exemple :**
```
Adapter : "junit-to-pytest-translator"
Description : "Convertit les fichiers de tests JUnit en tests pytest..."

Record A : "@Test annotation in JUnit 5"  → similarité 0.31 → CONSERVÉ
Record B : "Spring Boot DataSource config" → similarité 0.02 → REJETÉ (hors-sujet)
```

**Seuil :** 0.05 (intentionnellement bas — la comparaison code/prose donne naturellement des scores faibles)

**Sur un projet RH :** La description de l'adapter guide différemment le filtrage. Pour `hr-onboarding-qa` ("Répond aux questions des nouveaux employés sur la politique RH, les congés, les avantages sociaux..."), un record sur "les modalités de remboursement des frais de déplacement" obtient une similarité élevée, tandis qu'un article sur la fiscalité des entreprises est rejeté. Même mécanique, contexte différent.

**Sur un projet Legal Defense :** Le filtre pertinence est particulièrement strict — seuls les textes directement liés aux contrats et réglementations militaires passent. Zéro tolérance pour les documents génériques.

---

#### Filtre 5 — Contrôle du volume

Chaque adapter a un nombre cible de données. Ce filtre équilibre les volumes :

| Situation | Action |
|---|---|
| Volume > 110% de la cible | Garder les meilleurs par score, supprimer le reste |
| Volume entre 50% et 110% | Ne rien faire |
| Volume < 50% de la cible | Déclencher l'augmentation automatique |

**Augmentation automatique** (inspirée de la computer vision) :

Quand les données sont insuffisantes, on crée des variantes syntaxiquement équivalentes :

1. **Renommage d'identifiers** : `bubble_sort` → `var_0`, `arr` → `var_1`... Le modèle apprend la *logique*, pas les noms.

```python
# Original
def bubble_sort(arr):
    n = len(arr)
    for i in range(n):
        for j in range(n-i-1):
            if arr[j] > arr[j+1]:
                arr[j], arr[j+1] = arr[j+1], arr[j]

# Variante générée (AST-validée)
def var_0(var_1):
    var_2 = len(var_1)
    for var_3 in range(var_2):
        for var_4 in range(var_2-var_3-1):
            if var_1[var_4] > var_1[var_4+1]:
                var_1[var_4], var_1[var_4+1] = var_1[var_4+1], var_1[var_4]
```

2. **Échange de style de boucle** : `for i in range(n)` → `while`
3. **Paraphrase de description** : réordonne les phrases

**Garantie :** Toute variante de code est vérifiée par `ast.parse()` Python — si la syntaxe est invalide, la variante est rejetée.

**Sur un projet text (RH, Legal) :** Les techniques d'augmentation sont différentes — il n'y a pas de code à transformer. On utilise à la place : (1) **paraphrase de question** ("Combien de RTT ?" → "Quel est mon nombre de jours RTT annuels ?"), (2) **reformulation de réponse** en conservant le sens et les références légales, (3) **variation de niveau de détail** (réponse courte vs réponse avec contexte). Pas de vérification AST — la vérification porte sur la longueur et la cohérence sémantique avec la question.

---

#### Filtre 6 — Analyse de couverture thématique

Un clustering K-means (8 groupes) détecte si certains thèmes sont sous-représentés. Les records des thèmes manquants sont priorisés lors de l'augmentation.

**Exemple :** Si l'adapter `jpa-hibernate` a beaucoup d'exemples sur les relations OneToMany mais aucun sur l'héritage JPA, le clustering le détecte et priorise les variants sur l'héritage.

---

#### Filtre 7 — Réassignation inter-adapters

Un record collecté dans le mauvais adapter est automatiquement déplacé vers le bon.

**Exemple :**
```
Record "Spring @Entity annotation" → collecté pour java-to-python-translator
Similarité avec java-to-python : 0.31
Similarité avec jpa-hibernate   : 0.67
Marge = 0.36 > 0.15 → DÉPLACÉ vers jpa-hibernate-to-sqlalchemy-migrator
```

**Condition :** La marge doit être > 0.15 pour éviter les déplacements sur ambiguïté.

---

### Résultat du nettoyage

**Entrée :** 15 250 records bruts  
**Sortie :** 2 770 records de qualité, répartis par adapter dans `processed/clean_raw/`

Le taux de rejet élevé (~82%) est *voulu* — nous préférons 500 données excellentes à 5 000 données médiocres.

---

## Partie 3 — Formation des Paires d'Entraînement

### Qu'est-ce qu'une paire d'entraînement ?

Le fine-tuning fonctionne par exemples : on montre au modèle un input et l'output attendu. Pour la migration Java→Python :

```
Input  (prompt)     : "Migrate the following java code to python.\n\npublic class Sort..."
Output (completion) : "def sort(arr):\n    ..."
```

Cette paire d'entraînement enseigne au modèle la transformation attendue.

### Trois stratégies selon les données disponibles

#### Stratégie A — Appariement Rosetta Code (code_migration)

**Quand :** Les records Rosetta Code ont un `task_name` identique en Java et Python.

**Comment :** Jointure exacte sur le nom de la tâche.

```
Java  : task_name="Fibonacci", lang="java",   code="public static int fib..."
Python: task_name="Fibonacci", lang="python", code="def fib(n):..."

→ Paire : {prompt: "Migrate... java...\n```java\npublic static int fib...", 
            completion: "def fib(n):..."}
```

**Garantie :** 100% de pertinence — même algorithme dans les deux langues.

---

#### Stratégie B — Matching sémantique (semantic_pair_match)

**Quand :** Des pages de documentation traitent le même concept en Java et Python, mais sans identifiant commun.

**Comment :**
1. Encoder toutes les descriptions en vecteurs numériques (embeddings) **en un seul appel** (crucial pour partager le même espace vectoriel)
2. Calculer la similarité cosinus entre chaque description Java et chaque description Python
3. Si similarité ≥ 0.75 → les codes correspondants forment une paire
4. Contrainte : chaque code Python ne peut être utilisé qu'une seule fois (bijection)

**Boost :** +0.05 par mot-clé conceptuel commun dans les deux descriptions (ex. "class", "inheritance", "exception")

**Exemple :**
```
Description Java   : "La méthode concat() ajoute une chaîne à la fin..."
                     code: String s1 = "Hello "; s1.concat("World")

Description Python : "La concaténation de chaînes avec l'opérateur + permet..."
                     code: result = "Hello " + "World"

Similarité cosinus : 0.87 > 0.75 → PAIRE ACCEPTÉE
```

**Vérifications post-matching :**
- Ratio longueur : 0.10 ≤ len(code_cible) / len(code_source) ≤ 10.0
- Syntaxe Python : `ast.parse()` — rejeté si SyntaxError
- Syntaxe Java : mots-clés structurels + accolades équilibrées

---

#### Stratégie C — Génération directe (doc_to_code)

**Quand :** L'adapter travaille en monolangue (ex : quality-reviewer qui évalue du code Python).

**Comment :** La description devient l'instruction, le code devient la réponse attendue.

```
Input  : "Write python code that does the following:\n\nEvaluate the quality 
          of a Python migration by checking PEP 8 compliance..."
Output : "import ast\ndef check_pep8(code: str) -> dict:\n    ..."
```

---

#### Stratégie D — Q&A (template qa)

**Quand :** Domaine text — chatbot, FAQ, support. L'adapter répond à des questions en langage naturel.

**Comment :** La question devient le prompt, la réponse attendue devient la completion. Pas de syntaxe à vérifier.

```
Adapter : "hr-onboarding-qa"

Input  : "Combien de jours de RTT ai-je droit en tant que cadre ?"
Output : "Selon la convention collective Syntec, les cadres au forfait jour 
          bénéficient de 10 à 12 jours de RTT par an, selon le nombre de jours 
          travaillés convenu dans le contrat."
```

**Vérifications appliquées :** longueur de la réponse (≥ 30 mots pour être utile), présence d'une référence légale ou d'une source (fortement conseillée), cohérence sémantique question/réponse (similarité cosinus ≥ 0.40).

**Autre exemple — Legal Defense :**
```
Input  : "Un contrat de type MAPA peut-il être classifié Confidentiel Défense ?"
Output : "Non. Selon le Code de la Commande Publique, les MAPA (< 40 000 €) 
          ne peuvent pas porter de classification de défense. Un marché classifié 
          requiert une procédure formalisée avec habilitation préalable des 
          personnels impliqués."
```

---

### Génération synthétique — quand les sources naturelles sont insuffisantes

Si après collecte + matching, le nombre de paires est encore inférieur à la cible, nous générons les paires manquantes via un LLM (Bedrock Nova Pro).

**Principe :** Le LLM génère, le software décide si c'est valide.

**Trois modes de génération :**

1. **Traduction** (Java records disponibles) : le LLM traduit chaque snippet Java en Python idiomatique
2. **Synthétique** (pas de Java) : le LLM génère une paire Java+Python de zéro à partir de la description de l'adapter
3. **Doc-to-code** : le LLM génère des exemples description→code

**Toutes les paires générées passent les mêmes vérifications :**
- Syntaxe Python : `ast.parse()` — zéro tolérance pour les erreurs de syntaxe
- Syntaxe Java : regex structurel (mots-clés + accolades)
- Ratio longueur : 0.10–12.0
- Sauvegarde progressive toutes les 10 paires (pas de perte en cas d'interruption)
- Arrêt automatique après 8 échecs LLM consécutifs (détection de blocage)

**Coût estimé :** ~0.0038 $ par paire générée (Nova Pro, ~2 500 tokens/paire)

---

## Partie 4 — Nettoyage Final des Paires

### Pourquoi un second nettoyage ?

Les paires viennent de sources différentes (Rosetta Code, docs, génération LLM). Il peut y avoir des doublons inter-sources, des paires déséquilibrées ou de faible qualité. Ce nettoyage final garantit l'homogénéité.

### 5 étapes de finalisation

#### Étape 1 — Déduplication exacte des paires

Hash MD5 sur `prompt + completion`. Suppression des doublons exacts.

#### Étape 2 — Déduplication quasi-similaire (MinHash)

Même principe que la déduplication sémantique, mais sur les paires complètes (prompt + completion). Seuil : similarité Jaccard ≥ 0.85 sur des séquences de 5 tokens.

**Exemple :**
```
Paire A : prompt="Migrate the following java code..." completion="def fib(n):..."
Paire B : prompt="Migrate the following Java code..." completion="def fib(n):..."
→ Jaccard = 0.94 > 0.85 → QUASI-DOUBLON → Paire B supprimée
```

#### Étape 3 — Filtrage par règles métier

| Règle | Seuil | Rejet si |
|---|---|---|
| Longueur totale | 50–2048 tokens | Trop court ou trop long |
| Ratio completion/prompt | 0.3–10.0 | Completion vide ou gigantesque |
| Mots interdits | `password, secret, api_key, TODO, FIXME` | Contenu sensible ou incomplet |

#### Étape 4 — Score de qualité

Chaque paire reçoit un score :

**Score heuristique (rapide, gratuit) :**
- Ratio tokens 0.5–3.0 : +0.20
- Completion > 3 lignes : +0.15
- Mots-clés de code présents : +0.15
- Base : 0.50

**Score LLM (Bedrock, coût marginal) :** Bedrock note la paire de 0 à 10 sur la précision, la complétude et la cohérence.

Score final = 0.3 × heuristique + 0.7 × LLM

#### Étape 5 — Division train/validation

- **90%** → `train.jsonl` (données d'entraînement)
- **10%** → `valid.jsonl` (données de validation — jamais vues par le modèle)
- Mélange aléatoire déterministe (seed=42 → reproductible)

---

## Partie 5 — Garanties Qualité Transversales

### Ce qui est vérifié à chaque étape

| Menace | Protection | Étape |
|---|---|---|
| Doublons exacts | Hash MD5 | clean_raw + clean |
| Quasi-doublons | Cosinus ≥ 0.92 / MinHash 0.85 | clean_raw + clean |
| Code syntaxiquement invalide | `ast.parse()` Python, regex Java | clean_raw + transform + clean |
| Données hors-sujet | Similarité vs description adapter | clean_raw |
| Déséquilibre thématique | K-means clustering | clean_raw |
| Mauvaise attribution adapter | Réassignation cosinus | clean_raw |
| Paires déséquilibrées | Ratio longueur | transform + clean |
| Données sensibles | Blacklist | clean |
| Code généré invalide | AST + ratio + parse | pair_generator |
| Génération bloquée | Max 8 échecs consécutifs | pair_generator |

### Backend d'embeddings — fiabilité garantie

Pour les comparaisons sémantiques, nous utilisons une chaîne de fallback automatique :

```
1er choix : AWS Bedrock Titan v2 (dim=1024, 20 workers parallèles)
       ↓ si indisponible
2e choix  : Ollama nomic-embed-text (dim=768, local)
       ↓ si indisponible
3e choix  : sentence-transformers (dim=768, subprocess local)
       ↓ si indisponible
Fallback  : TF-IDF sklearn (toujours disponible)
```

Si TF-IDF est utilisé, le scoring de pertinence est automatiquement désactivé (incompatible cross-corpus) — plutôt qu'un faux score.

---

## Partie 6 — Les 6 Adapters du Projet Migrator

Chaque adapter représente un composant spécifique du pipeline de migration. Les données sont séparées par adapter pour garantir la spécialisation du modèle.

| Adapter | Rôle | Stratégie | Cible | État |
|---|---|---|---|---|
| java-to-python-translator | Traduit le corps des classes Java en Python | Fusion bilingue | 900 | ✅ 900/900 |
| junit-to-pytest-translator | Convertit les tests JUnit en tests pytest | Fusion bilingue | 600 | ✅ 600/600 |
| python-migration-quality-reviewer | Évalue la qualité du code Python migré | Description→Code | 1 000 | ✅ 1000/1000 |
| spring-config-to-python-config-migrator | Migre les configs Spring Boot vers Python | Fusion bilingue | 1 000 | 🔄 400/1000 |
| jpa-hibernate-to-sqlalchemy-migrator | Traduit les entités JPA en modèles SQLAlchemy | Fusion bilingue | 1 060 | 🔄 400/1060 |
| migration-pipeline-orchestrator | Orchestre le pipeline LangGraph | Description→Code | 300 | ✅ 300/300 |

Ces 6 adapters couvrent le spectre complet du projet Migrator. Mais le même pipeline, avec une configuration différente, produit des adapters pour n'importe quel autre domaine. Pour un projet **RH Onboarding**, on aurait par exemple : `hr-onboarding-qa` (questions générales), `contract-summarizer` (résumé de contrat de travail), `policy-compliance-checker` (conformité avec la politique interne). Pour un projet **Legal Defense** : `contract-sensitivity-classifier`, `regulatory-text-qa`, `clause-extractor`. Même pipeline, configurations différentes — aucune ligne de code à modifier.

---

## Partie 7 — État Actuel et Prochaines Étapes

### Métriques au 9 juin 2026

```
ENTONNOIR DE DONNÉES
                                                    
Collecte brute     ████████████████████  15 250 records
                                                    
Après clean_raw    █████░░░░░░░░░░░░░░░   2 770 records  (18%)
                                                    
Paires formées     ██████████████░░░░░░   3 600 paires   (74% de 4 860)
                                                    
Fine-tuning ready  ▓▓▓▓░░░░░░░░░░░░░░░░   En cours...
```

### Ce qu'il reste à faire

1. **Compléter la génération** pour `spring-config` et `jpa-hibernate` (~2 350 paires manquantes, ~8 €)
2. **Lancer le nettoyage final** (clean) sur toutes les paires
3. **Contrôle qualité** : inspection d'un échantillon aléatoire (onglet "Contrôle qualité")
4. **Entraînement** dès que le modèle de base est disponible (bloqué ZScaler — whitelist en cours)

### Dépendance externe bloquante

Le modèle de base `Qwen2.5-Coder-14B-Instruct-4bit` (~8 GB) ne peut pas être téléchargé en raison du proxy ZScaler Capgemini qui intercepte les connexions HTTPS vers HuggingFace. Une demande de whitelist est en cours.

---

## Partie 8 — Ajouter un nouveau projet en 5 minutes

Créer un nouveau projet ne nécessite pas de toucher au code du pipeline. Il suffit de créer un fichier YAML de configuration dans `projects/<nom-projet>/config.yaml`.

### Ce que contient un fichier de config projet

```yaml
# projects/hr-onboarding/config.yaml
project_id: hr-onboarding
domain: text                        # "code" ou "text"
base_model: mistral-7b-instruct-4bit

adapters:
  - name: hr-onboarding-qa
    task_type: qa                   # "generation", "qa", "classification"
    target_pairs: 800
    description: >
      Répond aux questions des nouveaux employés Capgemini sur la politique RH :
      congés, RTT, avantages sociaux, procédures administratives, onboarding.
    sources:
      - type: documentation
        urls:
          - https://www.service-public.fr/particuliers/vosdroits/F2258
          - https://www.legifrance.gouv.fr/conv_coll/id/KALICONT000005635173
        crawl_depth: 3
        pages_per_site: 100
      - type: manual_ingest         # fichiers déposés localement (pas de crawl)
        path: datalake/raw/hr-onboarding/guides/
      - type: awa
        keywords:
          - "convention collective Syntec congés RTT"
          - "onboarding cadre avantages salariés France"
        trusted_domains:
          - service-public.fr
          - legifrance.gouv.fr
    pair_strategy: qa               # Stratégie D — question/réponse
    augmentation:
      enabled: true
      techniques:
        - question_paraphrase
        - answer_reformulation      # pas de renommage d'identifiers (domaine text)

  - name: contract-summarizer
    task_type: generation
    target_pairs: 400
    description: >
      Résume les contrats de travail en points clés lisibles pour le salarié.
    sources:
      - type: manual_ingest
        path: datalake/raw/hr-onboarding/contracts/
    pair_strategy: doc_to_code      # description → résumé attendu
```

### Ce qui se passe ensuite (automatique)

Une fois ce fichier créé, les commandes suivantes fonctionnent sans configuration supplémentaire :

```bash
/collect-web project=hr-onboarding     # collecte selon les sources déclarées
/clean-dataset project=hr-onboarding   # nettoyage avec critères text (pas code)
/extract-pairs project=hr-onboarding   # stratégie qa automatiquement sélectionnée
/train-lora project=hr-onboarding      # fine-tuning sur les paires générées
```

### Cas Legal Defense — contrainte souveraineté maximale

Pour un projet à données sensibles, on désactive toutes les sources web et on n'utilise que l'ingestion manuelle :

```yaml
# projects/legal-defense/config.yaml
project_id: legal-defense
domain: text
sovereignty: strict                  # désactive AWA + documentation crawl + Bedrock

adapters:
  - name: contract-sensitivity-classifier
    task_type: classification
    sources:
      - type: manual_ingest          # UNIQUEMENT ingestion locale
        path: datalake/raw/legal-defense/contracts-anonymized/
    pair_strategy: qa
    llm_generation: false            # interdit de générer via LLM externe
```

Avec `sovereignty: strict`, le pipeline n'émet aucune requête réseau externe — tout traitement se fait en local (embeddings via sentence-transformers, scoring heuristique uniquement).

---

## Annexe — Glossaire

| Terme | Définition |
|---|---|
| **Record** | Un enregistrement brut collecté : description + code + langue |
| **Paire** | Un exemple d'entraînement : prompt (input) + completion (output attendu) |
| **Embedding** | Représentation numérique d'un texte en vecteur (liste de nombres) permettant de mesurer la similarité sémantique |
| **Cosinus** | Mesure de similarité entre deux vecteurs (0 = pas de rapport, 1 = identique) |
| **AST** | Abstract Syntax Tree — arbre syntaxique du code, utilisé pour vérifier la syntaxe Python |
| **Fine-tuning** | Spécialisation d'un modèle pré-entraîné sur des données spécifiques |
| **LoRA** | Low-Rank Adaptation — technique de fine-tuning légère, ne modifie que quelques paramètres |
| **Adapter** | Module LoRA spécialisé pour une tâche spécifique (ex : junit→pytest) |
| **GIGO** | Garbage In, Garbage Out — mauvaises données → mauvais modèle |
| **clean_raw** | Étape 1 de nettoyage sur les records monolingues bruts |
| **clean** | Étape 2 de nettoyage sur les paires d'entraînement formées |

---

