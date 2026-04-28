<script lang="ts">
  import { goto } from '$app/navigation';
  import { createProject, advisorRecommend, getDataHealth, getRuns, launchEvaluation } from '$lib/api';
  import type { AdvisorRecommendation } from '$lib/types';
  import Topbar from '$lib/components/Topbar.svelte';
  import SovereigntyBadge from '$lib/components/SovereigntyBadge.svelte';

  // ── Étapes du wizard ──────────────────────────────────────────────────────
  // 0 Accueil  1 Besoin  2 Architecture  3 Projet  4 Données  5 Training  6 Évaluation  7 Livraison
  let step = $state(0);

  // Étape 1 — besoin
  let need       = $state('');
  let aiModel    = $state('us.anthropic.claude-sonnet-4-6');
  let analyzing  = $state(false);
  let analyzeErr = $state('');
  let recommendation: AdvisorRecommendation | null = $state(null);

  // Étape 3 — création projet
  let projName   = $state('');
  let projDomain = $state('code');
  let projClass  = $state('interne');
  let baseModel  = $state('qwen2.5-coder-7b-4bit');
  let creating   = $state(false);
  let projectId  = $state('');
  let createErr  = $state('');

  // Étape 4 — données
  let dataReady  = $state(false);
  let pairCount  = $state(0);

  // Étape 5 — training
  let runId      = $state('');

  // Étape 6 — éval
  let evalLaunched = $state(false);

  const DOMAINS = ['code','legal','hr','finance','defense','healthcare','sales','compliance','other'];
  const BASE_MODELS = [
    { id: 'qwen2.5-coder-7b-4bit', label: 'Qwen 2.5 Coder 7B (recommandé)' },
    { id: 'mistral-7b-instruct-4bit', label: 'Mistral 7B Instruct' },
    { id: 'qwen2.5-14b-instruct-4bit', label: 'Qwen 2.5 14B (plus lent)' },
  ];

  const STEPS = [
    { num: 1, label: 'Besoin client' },
    { num: 2, label: 'Architecture' },
    { num: 3, label: 'Créer le projet' },
    { num: 4, label: 'Données' },
    { num: 5, label: 'Training' },
    { num: 6, label: 'Évaluation' },
    { num: 7, label: 'Livraison' },
  ];

  // ── Actions ───────────────────────────────────────────────────────────────
  async function analyzeNeed() {
    if (!need.trim()) return;
    analyzing = true; analyzeErr = ''; recommendation = null;
    try {
      recommendation = await advisorRecommend(need.trim(), aiModel);
      // Pre-fill project name from first adapter
      const firstAdapter = recommendation.recommendation.adapters[0];
      if (firstAdapter && !projName) {
        projName = firstAdapter.name.split('-').slice(0, 2).join('-');
      }
      step = 2;
    } catch (e) { analyzeErr = (e as Error).message; }
    finally { analyzing = false; }
  }

  async function createProj() {
    if (!projName.trim()) return;
    creating = true; createErr = '';
    try {
      const proj = await createProject({
        name: projName.trim(),
        domain: projDomain,
        classification: projClass,
        model: baseModel,
        model_format: baseModel.includes('qwen') ? 'qwen' : 'mistral',
        description: need.trim().slice(0, 200),
      });
      projectId = (proj as { id: string }).id;
      step = 4;
    } catch (e) { createErr = (e as Error).message; }
    finally { creating = false; }
  }

  async function checkDataReady() {
    if (!projectId) return;
    try {
      const health = await getDataHealth(projectId);
      pairCount = health.stages.fine_tuning_ready.count;
      dataReady = pairCount >= 200;
    } catch { dataReady = false; }
  }

  async function launchEval() {
    if (!runId) return;
    try {
      await launchEvaluation(runId);
      evalLaunched = true;
    } catch { /* non-blocking */ }
  }
</script>

<Topbar crumbs={[{ label: 'Démarrer un projet' }]}>
  <SovereigntyBadge mode="local" />
</Topbar>

<div class="content wizard-content">

  <!-- Étape 0 : accueil -->
  {#if step === 0}
    <div class="welcome">
      <div class="welcome-icon">⬡</div>
      <h1 class="welcome-title">Bienvenue dans MLOps Factory</h1>
      <p class="welcome-sub">
        Ce wizard vous guide de <strong>l'analyse du besoin client</strong> jusqu'à la
        <strong>livraison du modèle</strong>, étape par étape.<br>
        Aucune navigation dans le menu n'est nécessaire.
      </p>
      <div class="welcome-steps">
        {#each STEPS as s}
          <div class="ws-item">
            <div class="ws-num">{s.num}</div>
            <span class="ws-label">{s.label}</span>
          </div>
          {#if s.num < STEPS.length}<div class="ws-arrow">→</div>{/if}
        {/each}
      </div>
      <button class="btn btn-primary btn-lg" onclick={() => step = 1}>
        Commencer →
      </button>
    </div>

  <!-- Étape 1 : besoin client -->
  {:else if step === 1}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">1 / {STEPS.length}</div>
        <h2 class="wc-title">Analysez le besoin client</h2>
        <p class="wc-sub">Décrivez le projet en langage naturel. L'IA recommande l'architecture d'adapters optimale.</p>
        <SovereigntyBadge mode="cloud" label="🟠 Bedrock utilisé pour cette étape uniquement" />
      </div>

      <div class="wc-body">
        <label class="form-label">Besoin client</label>
        <textarea
          class="form-textarea"
          rows={6}
          bind:value={need}
          placeholder="Ex: Système d'aide à la rédaction de contrats pour un cabinet d'avocats en droit des affaires…"
          disabled={analyzing}
        ></textarea>

        <div class="form-group mt-12">
          <label class="form-label">Modèle d'analyse</label>
          <select class="form-select" bind:value={aiModel} disabled={analyzing}>
            <option value="us.anthropic.claude-sonnet-4-6">Sonnet 4.6 (recommandé)</option>
            <option value="us.anthropic.claude-opus-4-7">Opus 4.7 (plus précis)</option>
            <option value="us.anthropic.claude-haiku-4-5-20251001-v1:0">Haiku 4.5 (rapide)</option>
          </select>
        </div>

        {#if analyzeErr}<div class="api-error mt-8">{analyzeErr}</div>{/if}
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 0}>← Retour</button>
        <button
          class="btn btn-primary"
          disabled={analyzing || !need.trim()}
          onclick={analyzeNeed}
        >
          {#if analyzing}<span class="spinner"></span> Analyse en cours…{:else}Analyser →{/if}
        </button>
        <button class="btn btn-ghost skip-btn" onclick={() => step = 3}>
          Passer (projet sans analyse)
        </button>
      </div>
    </div>

  <!-- Étape 2 : résultat architecture -->
  {:else if step === 2 && recommendation}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">2 / {STEPS.length}</div>
        <h2 class="wc-title">Architecture recommandée</h2>
        <p class="wc-sub">Voici les adapters LoRA proposés. Vous pouvez les ajuster dans l'Advisor complet.</p>
      </div>

      <div class="wc-body">
        <div class="arch-summary">
          <div class="arch-stat">
            <span class="arch-num">{recommendation.recommendation.adapters.length}</span>
            <span class="arch-lbl">Spécialistes</span>
          </div>
          <div class="arch-stat">
            <span class="arch-num">1</span>
            <span class="arch-lbl">Orchestrateur</span>
          </div>
          <div class="arch-stat">
            <span class="arch-num">~{recommendation.total_estimated_pairs.toLocaleString()}</span>
            <span class="arch-lbl">Paires estimées</span>
          </div>
        </div>

        <div class="adapter-list mt-16">
          {#each recommendation.recommendation.adapters as a}
            <div class="adapter-row">
              <span class="adapter-name mono">{a.name}</span>
              <span class="adapter-desc text-muted">{a.description}</span>
              <span class="adapter-pairs">~{a.estimated_pairs}</span>
            </div>
          {/each}
          <div class="adapter-row adapter-orch">
            <span class="adapter-name mono">{recommendation.recommendation.orchestrator.name}</span>
            <span class="adapter-desc text-muted">{recommendation.recommendation.orchestrator.description}</span>
            <span class="badge badge-completed">orchestrateur</span>
          </div>
        </div>

        {#if recommendation.recommendation.notes}
          <div class="arch-notes mt-12">{recommendation.recommendation.notes}</div>
        {/if}
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 1}>← Modifier le besoin</button>
        <a href="/advisor" class="btn btn-ghost">Ouvrir l'Advisor complet</a>
        <button class="btn btn-primary" onclick={() => step = 3}>Créer le projet →</button>
      </div>
    </div>

  <!-- Étape 3 : créer le projet -->
  {:else if step === 3}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">3 / {STEPS.length}</div>
        <h2 class="wc-title">Créer le projet</h2>
        <p class="wc-sub">Configurez les paramètres de base. Vous pourrez tout modifier ensuite.</p>
      </div>

      <div class="wc-body">
        <div class="form-group">
          <label class="form-label" for="proj-name">Nom du projet</label>
          <input id="proj-name" class="form-input" bind:value={projName} placeholder="ex: legal-contracts" />
        </div>
        <div class="grid-2 mt-12">
          <div class="form-group">
            <label class="form-label" for="proj-domain">Domaine</label>
            <select id="proj-domain" class="form-select" bind:value={projDomain}>
              {#each DOMAINS as d}<option value={d}>{d}</option>{/each}
            </select>
          </div>
          <div class="form-group">
            <label class="form-label" for="proj-class">Classification</label>
            <select id="proj-class" class="form-select" bind:value={projClass}>
              <option value="public">public</option>
              <option value="interne">interne</option>
              <option value="confidentiel">confidentiel</option>
              <option value="secret">secret</option>
            </select>
          </div>
        </div>
        <div class="form-group mt-12">
          <label class="form-label" for="base-model">Modèle de base</label>
          <select id="base-model" class="form-select" bind:value={baseModel}>
            {#each BASE_MODELS as m}<option value={m.id}>{m.label}</option>{/each}
          </select>
        </div>
        {#if createErr}<div class="api-error mt-8">{createErr}</div>{/if}
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = recommendation ? 2 : 1}>← Retour</button>
        <button
          class="btn btn-primary"
          disabled={creating || !projName.trim()}
          onclick={createProj}
        >
          {creating ? 'Création…' : 'Créer et continuer →'}
        </button>
      </div>
    </div>

  <!-- Étape 4 : données -->
  {:else if step === 4}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">4 / {STEPS.length}</div>
        <h2 class="wc-title">Préparer les données</h2>
        <p class="wc-sub">
          Le projet <span class="mono text-accent">{projectId}</span> est créé.
          Ingérez et nettoyez vos paires d'entraînement avant de lancer le fine-tuning.
        </p>
      </div>

      <div class="wc-body">
        <div class="data-checklist">
          <div class="dc-item">
            <span class="dc-icon">◧</span>
            <div class="dc-text">
              <div class="dc-label">Ingestion des sources</div>
              <div class="dc-sub">Copiez vos fichiers dans <span class="mono">datalake/projects/{projectId}/raw/</span></div>
            </div>
            <a href="/data/ingest?project={projectId}" class="btn btn-ghost btn-sm">Ouvrir →</a>
          </div>
          <div class="dc-item">
            <span class="dc-icon">⟳</span>
            <div class="dc-text">
              <div class="dc-label">Pipeline de nettoyage</div>
              <div class="dc-sub">Score qualité, déduplication, filtrage des tokens</div>
            </div>
            <a href="/data/pipeline?project={projectId}" class="btn btn-ghost btn-sm">Ouvrir →</a>
          </div>
          <div class="dc-item">
            <span class="dc-icon">◩</span>
            <div class="dc-text">
              <div class="dc-label">Collecte web (optionnel)</div>
              <div class="dc-sub">Enrichissement automatique via recherche ciblée</div>
            </div>
            <a href="/data/collect?project={projectId}" class="btn btn-ghost btn-sm">Ouvrir →</a>
          </div>
        </div>

        {#if !dataReady}
          <div class="data-check-row mt-16">
            <button class="btn btn-ghost" onclick={checkDataReady}>↻ Vérifier l'état des données</button>
            {#if pairCount > 0}
              <span class="text-muted" style="font-size:var(--text-sm)">
                {pairCount} paires prêtes {pairCount < 200 ? '(minimum 200 requis)' : ''}
              </span>
            {/if}
          </div>
        {:else}
          <div class="data-ok mt-16">
            <span class="text-green">✓</span> {pairCount} paires fine-tuning-ready — prêt pour le training.
          </div>
        {/if}
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 3}>← Retour</button>
        <button class="btn btn-primary" onclick={() => step = 5}>
          Continuer vers le training →
        </button>
      </div>
    </div>

  <!-- Étape 5 : training -->
  {:else if step === 5}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">5 / {STEPS.length}</div>
        <h2 class="wc-title">Lancer le fine-tuning</h2>
        <p class="wc-sub">Configurez et lancez un run QLoRA sur votre dataset.</p>
      </div>

      <div class="wc-body">
        <div class="action-card">
          <div class="ac-icon">▷</div>
          <div class="ac-text">
            <div class="ac-title">Ouvrir le configurateur de training</div>
            <div class="ac-sub">Paramétrez les hyperparamètres (rank, LR, batch…) et lancez le run MLX.</div>
          </div>
          <a href="/train?project={projectId}" class="btn btn-primary">Ouvrir →</a>
        </div>

        <div class="form-group mt-16">
          <label class="form-label" for="run-id">ID du run lancé (à coller après lancement)</label>
          <input
            id="run-id"
            class="form-input mono"
            bind:value={runId}
            placeholder="ex: a1b2c3d4"
          />
          <div class="form-hint">Visible dans /train → historique des runs</div>
        </div>
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 4}>← Retour</button>
        <button
          class="btn btn-primary"
          disabled={!runId.trim()}
          onclick={() => step = 6}
        >
          Run lancé, continuer →
        </button>
      </div>
    </div>

  <!-- Étape 6 : évaluation -->
  {:else if step === 6}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">6 / {STEPS.length}</div>
        <h2 class="wc-title">Évaluer le modèle</h2>
        <p class="wc-sub">Évaluation obligatoire avant déploiement — BLEU, ROUGE-L, pass rate, latence.</p>
      </div>

      <div class="wc-body">
        {#if !evalLaunched}
          <div class="action-card">
            <div class="ac-icon">◉</div>
            <div class="ac-text">
              <div class="ac-title">Lancer l'évaluation du run <span class="mono text-accent">{runId}</span></div>
              <div class="ac-sub">Compare votre modèle au baseline Bedrock Nova Pro.</div>
            </div>
            <button class="btn btn-primary" onclick={launchEval}>▷ Évaluer</button>
          </div>
          <div class="action-card mt-12">
            <div class="ac-icon">◐</div>
            <div class="ac-text">
              <div class="ac-title">Tester dans le Playground</div>
              <div class="ac-sub">Envoyez des prompts manuels pour valider la qualité.</div>
            </div>
            <a href="/playground?project={projectId}" class="btn btn-ghost">Ouvrir →</a>
          </div>
        {:else}
          <div class="data-ok">
            <span class="text-green">✓</span> Évaluation lancée pour le run <span class="mono">{runId}</span>.
          </div>
          <a href="/eval" class="btn btn-ghost mt-12">Voir les résultats →</a>
        {/if}
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 5}>← Retour</button>
        <button class="btn btn-primary" onclick={() => step = 7}>Continuer vers la livraison →</button>
      </div>
    </div>

  <!-- Étape 7 : livraison -->
  {:else if step === 7}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">7 / {STEPS.length}</div>
        <h2 class="wc-title">Livraison du modèle</h2>
        <p class="wc-sub">Fusionnez l'adapter, passez la gate qualité, promouvez en production.</p>
      </div>

      <div class="wc-body">
        <div class="action-card">
          <div class="ac-icon">⬡</div>
          <div class="ac-text">
            <div class="ac-title">Déployer le modèle</div>
            <div class="ac-sub">Fuse → staging → gate → production → serving port.</div>
          </div>
          <a href="/delivery?project={projectId}" class="btn btn-primary">Ouvrir →</a>
        </div>

        <div class="done-banner mt-24">
          <div class="done-icon">✓</div>
          <div>
            <div class="done-title">Vous avez complété toutes les étapes</div>
            <div class="done-sub">Le projet <span class="mono text-accent">{projectId}</span> est prêt pour la mise en production.</div>
          </div>
        </div>
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 6}>← Retour</button>
        <button class="btn btn-ghost" onclick={() => goto(`/projects/${projectId}`)}>Voir le projet</button>
        <button class="btn btn-primary" onclick={() => { step = 0; need = ''; projName = ''; projectId = ''; runId = ''; recommendation = null; evalLaunched = false; }}>
          ↺ Nouveau projet
        </button>
      </div>
    </div>
  {/if}

  <!-- Barre de progression -->
  {#if step > 0}
    <div class="progress-bar-wrap">
      <div class="progress-bar" style="width:{(step / STEPS.length) * 100}%"></div>
    </div>
  {/if}

</div>

<style>
  .wizard-content {
    max-width: 860px;
    margin: 0 auto;
    width: 100%;
  }

  /* Welcome */
  .welcome {
    display: flex; flex-direction: column; align-items: center;
    text-align: center; padding: var(--sp-10) var(--sp-6);
    gap: 20px;
  }
  .welcome-icon { font-size: 40px; }
  .welcome-title { font-size: var(--text-2xl); font-weight: 700; }
  .welcome-sub { font-size: var(--text-base); color: var(--text-muted); line-height: 1.7; max-width: 560px; }
  .welcome-steps {
    display: flex; align-items: center; flex-wrap: wrap; justify-content: center;
    gap: 6px; margin: 8px 0;
  }
  .ws-item { display: flex; flex-direction: column; align-items: center; gap: 3px; }
  .ws-num { width: 24px; height: 24px; border-radius: 50%; background: var(--surface-2); border: 2px solid var(--border); display: flex; align-items: center; justify-content: center; font-size: var(--text-xs); font-weight: 700; color: var(--text-muted); }
  .ws-label { font-size: 10px; color: var(--text-dim); white-space: nowrap; }
  .ws-arrow { font-size: 12px; color: var(--text-dim); padding: 0 2px; }

  .btn-lg { padding: 12px 28px; font-size: var(--text-base); }

  /* Wizard card */
  .wizard-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }

  .wc-header {
    padding: 20px 24px 16px;
    border-bottom: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 6px;
  }
  .wc-step-badge {
    font-size: var(--text-xs); font-weight: 700;
    text-transform: uppercase; letter-spacing: .08em;
    color: var(--accent);
  }
  .wc-title { font-size: var(--text-xl); font-weight: 700; }
  .wc-sub   { font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; margin: 0; }

  .wc-body   { padding: 20px 24px; }
  .wc-footer {
    padding: 14px 24px;
    border-top: 1px solid var(--border);
    display: flex; align-items: center; gap: 10px;
    background: var(--surface-2);
  }

  .skip-btn { margin-left: auto; font-size: var(--text-xs); opacity: .7; }

  /* Architecture */
  .arch-summary { display: grid; grid-template-columns: repeat(3,1fr); gap: 10px; }
  .arch-stat { display: flex; flex-direction: column; align-items: center; padding: 12px; background: var(--surface-2); border-radius: var(--radius); gap: 3px; }
  .arch-num { font-size: var(--text-xl); font-weight: 700; color: var(--accent); }
  .arch-lbl { font-size: var(--text-xs); color: var(--text-muted); }
  .arch-notes { font-size: var(--text-sm); color: var(--text-muted); background: var(--surface-2); border-radius: var(--radius); padding: 10px 14px; font-style: italic; line-height: 1.5; }

  .adapter-list { display: flex; flex-direction: column; gap: 4px; }
  .adapter-row {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 10px; border-radius: var(--radius);
    background: var(--surface-2); border: 1px solid var(--border);
    font-size: var(--text-sm);
  }
  .adapter-orch { border-color: color-mix(in srgb, var(--accent) 40%, transparent); }
  .adapter-name { font-weight: 600; flex-shrink: 0; min-width: 140px; }
  .adapter-desc { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .adapter-pairs { font-size: var(--text-xs); color: var(--text-dim); flex-shrink: 0; }

  /* Data checklist */
  .data-checklist { display: flex; flex-direction: column; gap: 8px; }
  .dc-item {
    display: flex; align-items: center; gap: 12px;
    padding: 12px 14px; border-radius: var(--radius-md);
    background: var(--surface-2); border: 1px solid var(--border);
  }
  .dc-icon { font-size: 16px; color: var(--accent); flex-shrink: 0; }
  .dc-text { flex: 1; }
  .dc-label { font-weight: 500; font-size: var(--text-sm); }
  .dc-sub   { font-size: var(--text-xs); color: var(--text-muted); margin-top: 2px; }

  .data-check-row { display: flex; align-items: center; gap: 12px; }
  .data-ok { display: flex; align-items: center; gap: 8px; font-size: var(--text-sm); color: var(--text-muted); }

  /* Action cards */
  .action-card {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 16px; border-radius: var(--radius-md);
    background: var(--surface-2); border: 1px solid var(--border);
  }
  .ac-icon  { font-size: 20px; color: var(--accent); flex-shrink: 0; }
  .ac-text  { flex: 1; }
  .ac-title { font-weight: 500; font-size: var(--text-sm); }
  .ac-sub   { font-size: var(--text-xs); color: var(--text-muted); margin-top: 2px; }

  /* Done */
  .done-banner {
    display: flex; align-items: center; gap: 16px;
    padding: 20px; border-radius: var(--radius-md);
    background: var(--green-dim); border: 1px solid rgba(62,207,142,.3);
  }
  .done-icon  { font-size: 28px; color: var(--green); flex-shrink: 0; }
  .done-title { font-weight: 600; font-size: var(--text-base); color: var(--green); }
  .done-sub   { font-size: var(--text-sm); color: var(--text-muted); margin-top: 2px; }

  /* Progress bar */
  .progress-bar-wrap {
    height: 3px; background: var(--surface-3); border-radius: 2px;
    margin-top: 16px; overflow: hidden;
  }
  .progress-bar { height: 100%; background: var(--accent); border-radius: 2px; transition: width 400ms ease; }

  /* Form hint */
  .form-hint { font-size: var(--text-xs); color: var(--text-dim); margin-top: 4px; }

  .mt-12 { margin-top: 12px; }
  .mt-16 { margin-top: 16px; }
  .mt-24 { margin-top: 24px; }

  .api-error {
    background: var(--red-dim); border: 1px solid rgba(247,84,84,.3);
    border-radius: var(--radius); padding: 8px 12px;
    font-size: var(--text-sm); color: var(--red);
  }

  .btn-sm { padding: 4px 10px; font-size: var(--text-sm); font-weight: 500; }

  .spinner {
    display: inline-block; width: 12px; height: 12px;
    border: 2px solid currentColor; border-top-color: transparent;
    border-radius: 50%; animation: spin 600ms linear infinite;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
</style>
