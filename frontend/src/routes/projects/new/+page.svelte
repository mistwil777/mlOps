<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getBaseModels, getTaskTypes, getDomains, createProject } from '$lib/api';
  import type { BaseModel } from '$lib/types';
  import VramEstimator from '$lib/components/VramEstimator.svelte';

  let models: BaseModel[]   = $state([]);
  let taskTypes: string[]   = $state([]);
  let domains: string[]     = $state([]);
  let apiError = $state('');
  let error = $state('');
  let submitting = $state(false);

  // Form state
  let name           = $state('');
  let description    = $state('');
  let classification = $state('interne');
  let domain         = $state('code');
  let task_type      = $state('generation');
  let model_id       = $state('');
  let min_pass       = $state(0.85);
  let min_rouge      = $state(0.65);
  let max_lat        = $state(2.0);
  let min_tokens     = $state(50);
  let max_tokens     = $state(2048);
  let blacklist      = $state('password\nsecret\napi_key\nTODO\nFIXME');

  const selectedModel = $derived(models.find(m => m.id === model_id) ?? models[0]);

  const previewYaml = $derived(`name: ${name || '<nom>'}
description: ${description || ''}
classification: ${classification}
domain: ${domain}
task_type: ${task_type}
model: ${selectedModel?.name ?? model_id}
model_format: ${selectedModel?.format ?? 'qwen'}
quality_thresholds:
  min_pass_rate: ${min_pass}
  min_rouge_l: ${min_rouge}
  max_latency_ratio: ${max_lat}
min_tokens: ${min_tokens}
max_tokens: ${max_tokens}`);

  onMount(async () => {
    try {
      [models, taskTypes, domains] = await Promise.all([getBaseModels(), getTaskTypes(), getDomains()]);
      if (models.length) model_id = models[0].id;
    } catch {
      apiError = 'API indisponible (localhost:7860) — démarrer avec : uvicorn api.main:app --port 7860';
    }
  });

  async function submit() {
    if (!name.trim()) { error = 'Le nom est requis'; return; }
    submitting = true;
    error = '';
    try {
      const p = await createProject({
        name, description, classification, domain, task_type,
        model: selectedModel?.name ?? model_id,
        model_format: selectedModel?.format ?? 'qwen',
        quality_thresholds: { min_pass_rate: min_pass, min_rouge_l: min_rouge, max_latency_ratio: max_lat },
        min_tokens, max_tokens,
        blacklist_patterns: blacklist.split('\n').map(s => s.trim()).filter(Boolean),
      });
      goto(`/projects/${p.id}`);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      submitting = false;
    }
  }
</script>

<div class="topbar">
  <span class="topbar-title">Nouveau projet — Cahier des charges</span>
</div>

<div class="content">
  {#if apiError}
    <div class="api-error mb-16">{apiError}</div>
  {/if}
  <div class="cdc-layout">
    <!-- Form -->
    <div class="cdc-form">
      <div class="card mb-16">
        <div class="card-title mb-16">① Identité</div>
        <div class="form-group">
          <label class="form-label">Nom du projet *</label>
          <input class="form-input" bind:value={name} placeholder="ex: migrator, code-review…" />
        </div>
        <div class="form-group">
          <label class="form-label">Description</label>
          <textarea class="form-textarea" bind:value={description} placeholder="Objectif du fine-tuning…"></textarea>
        </div>
        <div class="form-group">
          <label class="form-label">Classification</label>
          <select class="form-select" bind:value={classification}>
            <option>interne</option>
            <option>confidentiel</option>
            <option>secret</option>
          </select>
        </div>
      </div>

      <div class="card mb-16">
        <div class="card-title mb-16">② Domaine & tâche</div>
        <div class="grid-2">
          <div class="form-group">
            <label class="form-label">Domaine</label>
            <select class="form-select" bind:value={domain} disabled={domains.length === 0}>
              {#if domains.length === 0}
                <option value="">API indisponible…</option>
              {:else}
                {#each domains as d}<option value={d}>{d}</option>{/each}
              {/if}
            </select>
          </div>
          <div class="form-group">
            <label class="form-label">Type de tâche</label>
            <select class="form-select" bind:value={task_type} disabled={taskTypes.length === 0}>
              {#if taskTypes.length === 0}
                <option value="">API indisponible…</option>
              {:else}
                {#each taskTypes as t}<option value={t}>{t}</option>{/each}
              {/if}
            </select>
          </div>
        </div>
      </div>

      <div class="card mb-16">
        <div class="card-title mb-16">③ Modèle de base</div>
        <div class="model-cards">
          {#each models as m}
            <div class="model-card {model_id === m.id ? 'selected' : ''}" onclick={() => model_id = m.id} role="radio" aria-checked={model_id === m.id} tabindex="0">
              <div class="model-name">{m.name}</div>
              <div class="model-meta">
                <span class="badge badge-completed">{m.format}</span>
                <span class="text-muted">{m.vram_gb} GB VRAM</span>
              </div>
              <div class="model-path mono text-muted">{m.hf_path}</div>
            </div>
          {/each}
        </div>
        {#if selectedModel}
          <div class="mt-16">
            <VramEstimator vramBase={selectedModel.vram_gb} batchSize={selectedModel.training?.batch_size ?? 8} loraRank={16} />
          </div>
        {/if}
      </div>

      <div class="card mb-16">
        <div class="card-title mb-16">④ Seuils de qualité</div>
        <div class="form-group">
          <label class="form-label">Pass rate minimum: <strong>{min_pass}</strong></label>
          <input type="range" min="0.5" max="1" step="0.05" bind:value={min_pass} />
        </div>
        <div class="form-group">
          <label class="form-label">Rouge-L minimum: <strong>{min_rouge}</strong></label>
          <input type="range" min="0.3" max="1" step="0.05" bind:value={min_rouge} />
        </div>
        <div class="form-group">
          <label class="form-label">Ratio latence max (vs baseline): <strong>{max_lat}×</strong></label>
          <input type="range" min="1" max="5" step="0.5" bind:value={max_lat} />
        </div>
      </div>

      <div class="card mb-24">
        <div class="card-title mb-16">⑤ Filtres données</div>
        <div class="grid-2">
          <div class="form-group">
            <label class="form-label">Tokens min</label>
            <input type="number" class="form-input" bind:value={min_tokens} min="10" />
          </div>
          <div class="form-group">
            <label class="form-label">Tokens max</label>
            <input type="number" class="form-input" bind:value={max_tokens} max="8192" />
          </div>
        </div>
        <div class="form-group">
          <label class="form-label">Mots à exclure du dataset</label>
          <p class="form-hint">Toute paire d'entraînement contenant l'un de ces mots sera rejetée automatiquement lors du nettoyage. Un mot par ligne. Protège contre l'injection de credentials ou de données sensibles dans le modèle.</p>
          <textarea class="form-textarea" bind:value={blacklist}></textarea>
        </div>
      </div>

      {#if error}<p class="text-red mb-16">{error}</p>{/if}
      <button class="btn btn-primary w-full" onclick={submit} disabled={submitting}>
        {submitting ? 'Création…' : '✓ Créer le projet'}
      </button>
    </div>

    <!-- YAML preview -->
    <div class="cdc-preview">
      <div class="card" style="position:sticky;top:24px">
        <div class="card-header">
          <div class="card-title">Preview config.yaml</div>
        </div>
        <pre class="yaml-preview">{previewYaml}</pre>
      </div>
    </div>
  </div>
</div>

<style>
  .cdc-layout { display: grid; grid-template-columns: 1fr 380px; gap: 24px; align-items: start; }
  .cdc-form { min-width: 0; }
  .cdc-preview { min-width: 0; }
  .yaml-preview {
    background: #0a0c12;
    padding: 14px;
    border-radius: var(--radius);
    font-family: var(--font-mono);
    font-size: 12px;
    line-height: 1.8;
    color: #a8b0c8;
    white-space: pre-wrap;
    overflow-x: auto;
  }
  .model-cards { display: flex; flex-direction: column; gap: 10px; }
  .model-card {
    padding: 12px 16px;
    border: 2px solid var(--border);
    border-radius: var(--radius);
    cursor: pointer;
    transition: border-color 200ms, background 200ms;
  }
  .model-card:hover    { background: var(--surface-2); }
  .model-card.selected { border-color: var(--accent); background: var(--accent-dim); }
  .model-name { font-weight: 600; margin-bottom: 6px; }
  .model-meta { display: flex; gap: 10px; align-items: center; margin-bottom: 4px; font-size: 12px; }
  .model-path { font-size: 11px; }
  .form-hint { font-size: 12px; color: var(--text-muted); margin: 4px 0 8px; line-height: 1.5; }
  .api-error { background: rgba(255,80,80,0.1); border: 1px solid var(--red); border-radius: var(--radius); padding: 10px 14px; font-size: 13px; color: var(--red); font-family: var(--font-mono); }
  @media (max-width: 900px) { .cdc-layout { grid-template-columns: 1fr; } .cdc-preview { display: none; } }
</style>
