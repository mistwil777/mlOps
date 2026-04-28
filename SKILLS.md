# SKILLS.md — Versioned skill registry

Convention de commit lors de toute modification : `skill/<name>/vX.Y`
Incrémenter MAJOR si changement de comportement observable (breaking). MINOR sinon.

| Skill | Version | Date | Breaking changes |
|---|---|---|---|
| /new-project | v1.0 | 2026-04-28 | — |
| /extract-pairs | v1.0 | 2026-04-28 | — |
| /clean-dataset | v1.0 | 2026-04-28 | — |
| /augment-dataset | v1.0 | 2026-04-28 | — |
| /collect-web | v1.0 | 2026-04-28 | — |
| /train-lora | v1.0 | 2026-04-28 | — |
| /eval-model | v1.0 | 2026-04-28 | — |
| /deploy-model | v1.1 | 2026-04-28 | Fuse output → models/staging/. Gate obligatoire avant /serve. |
| /compare-models | v1.0 | 2026-04-28 | — |
| /benchmark-all | v1.0 | 2026-04-28 | — |
| /daily-report | v1.0 | 2026-04-28 | — |
| /advisor | v1.0 | 2026-04-28 | Nouveau — scoping multi-adapter |
| /orchestrate | v1.0 | 2026-04-28 | Nouveau — routing + synthèse multi-adapter |
