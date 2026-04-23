# Projet Migrator

Migration automatique de code Python vers Java.

## Source
Repo source : `~/migrator/` (lecture seule)
Voir [MIGRATION.md](../../MIGRATION.md) pour le déplacement prévu vers `~/Documents/GitHub/migrator/`.

## Stack
- Modèle : Mistral 7B Instruct v0.3 4-bit
- Baseline : AWS Bedrock Nova Pro (us.amazon.nova-pro-v1:0)
- Classification : Interne

## Objectifs qualité
- Taux de compilation Java : ≥ 90%
- CodeBLEU : ≥ 0.70
- Latence p50 : ≤ 2× Nova Pro

## Lancer le pipeline
```
/extract-pairs project=migrator repo=~/migrator
/clean-dataset project=migrator score=true threshold=0.7
/train-lora project=migrator iters=1000
/eval-model project=migrator adapter=adapters/migrator/<run_id>/ baseline=nova-pro
/deploy-model project=migrator adapter=adapters/migrator/<run_id>/
```
