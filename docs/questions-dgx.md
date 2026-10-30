# Questions DGX — Accès infrastructure GPU

Document préparé pour faciliter les échanges avec l'équipe infrastructure.

---

## Contexte

Dans le cadre du projet MLOps, nous avons besoin d'accéder aux serveurs DGX pour
entraîner et déployer des modèles de langage spécialisés (fine-tuning LoRA).
Les réponses ci-dessous déterminent entièrement notre architecture technique.

---

## Questions à poser

### 1. Type d'accès

> Les serveurs DGX sont-ils accessibles en **inférence seulement** ou en **écriture (fine-tuning)** ?

- **Inférence seulement** : le serveur fait tourner un modèle pour répondre à des requêtes. Les poids ne changent pas.
- **Fine-tuning (écriture)** : on peut modifier les poids d'un modèle sur le serveur (entraînement PyTorch). C'est ce dont on a besoin pour spécialiser les modèles.

---

### 2. Format des modèles disponibles

> Les modèles déjà présents sur les serveurs sont-ils en format **GGUF** ou **Safetensors** ?

- **GGUF** : format de déploiement (Ollama, llama.cpp) — lecture rapide, **non fine-tunable**
- **Safetensors** : format HuggingFace — **requis pour le fine-tuning**

Si les modèles sont uniquement en GGUF, il faudra télécharger les versions safetensors depuis HuggingFace pour entraîner (ex. Qwen2.5-32B BF16 = ~64 Go d'espace disque).

---

### 3. Bibliothèques disponibles

> Est-ce qu'**Unsloth** est installé sur les serveurs ?

```bash
pip show unsloth
```

Unsloth est la bibliothèque qu'on utilise pour le fine-tuning sur GPU NVIDIA. Elle est
2× plus rapide que HuggingFace natif et optimisée pour les H200.

Si non installé : peut-on l'installer ? (`pip install unsloth`)

---

### 4. PyTorch + CUDA

> PyTorch est-il disponible avec le support CUDA ?

```bash
python -c "import torch; print(torch.cuda.is_available())"
```

Doit retourner `True`. C'est le prérequis de base pour tout entraînement GPU.

**Qu'est-ce que CUDA ?**
CUDA (Compute Unified Device Architecture) est le langage créé par NVIDIA pour
programmer leurs GPUs. C'est le traducteur qui permet à Python/PyTorch de déléguer
les calculs au GPU au lieu du CPU. Sans CUDA actif, le GPU H200 est présent mais
inutilisé pour l'entraînement. C'est différent de l'environnement Mac (Apple Silicon)
qui utilise une technologie équivalente mais incompatible appelée MLX.

---

### 5. Mode d'accès

> Comment accède-t-on aux serveurs ?

- **SSH direct** : on se connecte et on lance les scripts directement
- **Job scheduler (SLURM)** : on soumet des jobs dans une queue, le scheduler alloue les GPUs

Les deux sont compatibles avec notre pipeline, mais la configuration diffère.

---

### 6. Quota et partage

> Y a-t-il un quota d'heures GPU alloué par projet ? Les serveurs sont-ils partagés avec d'autres équipes ?

Utile pour planifier les runs d'entraînement (un fine-tuning 32B prend ~2-4h sur H200).

---

## Pourquoi ces questions sont bloquantes

| Question | Si oui | Si non |
|----------|--------|--------|
| Accès fine-tuning | On entraîne sur H200, modèles 32B-72B | On entraîne sur Mac, limité à 7B 4-bit |
| Safetensors disponibles | Prêt à démarrer | Téléchargement HuggingFace nécessaire (~64 Go) |
| Unsloth installé | Pipeline prêt | Installation à demander |
| PyTorch CUDA | Prêt | Environnement à configurer |
| SSH direct | Intégration pipeline simple | Adaptation pour SLURM |

---

## Modèles dont on a besoin

Les modèles suivants sont approuvés Cybersécurité (Qwen + DeepSeek).
Selon le format des modèles déjà présents sur le DGX, certains seront à télécharger.

**Règle importante :** pour le fine-tuning, on a besoin des versions **Safetensors**
(HuggingFace), pas GGUF. Si les modèles existants sont en GGUF, il faut les versions
Safetensors en complément — ce sont deux fichiers différents du même modèle.

| Modèle | Série | Taille Safetensors | Usage | Priorité |
|--------|-------|--------------------|-------|----------|
| Qwen2.5-Coder-7B-Instruct | Qwen2.5 | ~14 Go BF16 / ~4 Go INT4 | Code, tests, docstrings (Mac + H200) | Haute |
| Qwen2.5-Coder-32B-Instruct | Qwen2.5 | ~64 Go BF16 / ~16 Go INT4 | Code avancé, revue sécurité (H200) | Haute |
| Qwen3-8B | Qwen3 | ~16 Go BF16 / ~4 Go INT4 | Rédaction, rapports, RH (Mac + H200) | Haute |
| Qwen3-32B | Qwen3 | ~64 Go BF16 / ~16 Go INT4 | Raisonnement, juridique, avant-vente (H200) | Haute |
| DeepSeek-Coder-V2-Lite-Instruct | DeepSeek | ~30 Go BF16 | Code legacy, analyse CVE (H200) | Moyenne |
| DeepSeek-R1-Distill-Qwen-32B | DeepSeek | ~64 Go BF16 | Raisonnement multi-étapes complexes (H200) | Moyenne |

**Pourquoi Qwen3 plutôt que Qwen2.5 pour les tâches générales :**
Qwen3 intègre un mode "thinking" (raisonnement pas à pas, comme GPT-o1) directement
dans le modèle de base. Plus performant sur les tâches complexes (juridique, conformité,
analyse de risque) tout en restant fine-tunable. Qwen2.5-Coder est conservé pour le code
car Qwen3-Coder n'est pas encore disponible.

**Espace disque estimé** pour l'ensemble en BF16 : ~252 Go.
En INT4 (quantization post-entraînement) : ~65 Go — suffisant si l'espace est limité,
mais le fine-tuning se fait toujours sur les poids BF16 avant quantization.

---

## Ce qu'on peut faire sans le DGX

En attendant les réponses, on peut démarrer le projet sur Mac (MacBook M4 Pro, 48 Go) :
- Fine-tuning de modèles 7B en 4-bit via MLX
- Génération et validation des datasets (Nova Pro + juge Claude)
- Tests du pipeline complet

Le DGX devient nécessaire dès qu'on veut entraîner des modèles 32B+ ou accélérer
les runs de production.
