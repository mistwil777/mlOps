<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { getProjects, getBaseModels, createRun } from '$lib/api';
  import type { Project, BaseModel } from '$lib/types';
  import VramEstimator from '$lib/components/VramEstimator.svelte';
  import SectionTabs from '$lib/components/SectionTabs.svelte';
  const tabs = [{ href: '/train', label: 'Fine-tuning' }, { href: '/runs', label: 'Runs' }];

  let projects: Project[]   = $state([]);
  let models: BaseModel[]   = $state([]);
  let error = $state('');
  let submitting = $state(false);

  // Form state
  let project_id     = $state($page.url.searchParams.get('project') ?? '');
  let model_id       = $state('');
  let experiment     = $state('');
  let iters          = $state(1000);
  let batch_size     = $state(8);
  let learning_rate  = $state(1e-4);
  let lora_rank      = $state(16);
  let lora_layers    = $state(16);
  let use_rag        = $state(true);
  let resume         = $state(false);

  const selectedModel = $derived(models.find(m => m.id === model_id) ?? models[0]);

  onMount(async () => {
    [projects, models] = await Promise.all([getProjects(), getBaseModels()]);
    if (models.length) model_id = models[0].id;
    if (!project_id && projects.length) project_id = projects[0].id;
  });

  async function submit() {
    if (!project_id) { error = 'Sélectionner un projet'; return; }
    submitting = true; error = '';
    try {
      const { run_id } = await createRun({
        project_id, experiment_name: experiment || `${project_id}-lora`,
        iters, batch_size, learning_rate, lora_rank, lora_layers: lora_layers,
        use_rag, resume,
      });
      goto(`/runs/${run_id}`);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      submitting = false;
    }
  }
</script>

<SectionTabs {tabs} />
<div class="topbar">
  <span class="topbar-title">Lancer un fine-tuning</span>
</div>

<div class="content">
  <div class="train-layout">
    <div class="train-form">
      <div class="card mb-16">
        <div class="card-title mb-16">① Projet</div>
        <select class="form-select" bind:value={project_id}>
          <option value="">Sélectionner…</option>
          {#each projects as p}
            <option value={p.id}>{p.name} [{p.domain}/{p.task_type}]</option>
          {/each}
        </select>
        <div class="form-group mt-16">
          <label class="form-label">Nom de l'expérience</label>
          <input class="form-input" bind:value={experiment} placeholder="{project_id || 'projet'}-lora" />
        </div>
      </div>

      <div class="card mb-16">
        <div class="card-title mb-16">② Modèle de base</div>
        <div class="model-cards">
          {#each models as m}
            <div class="model-card {model_id === m.id ? 'selected' : ''}" onclick={() => model_id = m.id} role="radio" aria-checked={model_id === m.id} tabindex="0">
              <div class="flex justify-between items-center">
                <div>
                  <div class="model-name">{m.name}</div>
                  <div class="model-path mono text-muted">{m.hf_path}</div>
                </div>
                <div class="text-right">
                  <div style="font-weight:700;color:var(--accent)">{m.vram_gb} GB</div>
                  <div class="badge badge-completed" style="margin-top:4px">{m.format}</div>
                </div>
              </div>
            </div>
          {/each}
        </div>
        {#if selectedModel}
          <div class="mt-16">
            <VramEstimator vramBase={selectedModel.vram_gb} batchSize={batch_size} loraRank={lora_rank} />
          </div>
        {/if}
      </div>

      <div class="card mb-16">
        <div class="card-title mb-16">③ Hyperparamètres LoRA</div>
        <div class="grid-2">
          <div class="form-group">
            <label class="form-label">Iters: <strong>{iters}</strong></label>
            <input type="range" min="100" max="5000" step="100" bind:value={iters} />
          </div>
          <div class="form-group">
            <label class="form-label">Batch size: <strong>{batch_size}</strong></label>
            <input type="range" min="1" max="16" step="1" bind:value={batch_size} />
          </div>
          <div class="form-group">
            <label class="form-label">Learning rate</label>
            <input type="number" class="form-input" bind:value={learning_rate} step="0.00001" />
          </div>
          <div class="form-group">
            <label class="form-label">LoRA rank: <strong>{lora_rank}</strong></label>
            <input type="range" min="4" max="64" step="4" bind:value={lora_rank} />
          </div>
          <div class="form-group">
            <label class="form-label">LoRA layers: <strong>{lora_layers}</strong></label>
            <input type="range" min="4" max="32" step="4" bind:value={lora_layers} />
          </div>
        </div>
      </div>

      <div class="card mb-24">
        <div class="card-title mb-16">④ Options</div>
        <label class="toggle-row">
          <input type="checkbox" bind:checked={use_rag} />
          <span>Activer RAG (enrichissement des paires avec LanceDB)</span>
        </label>
        <label class="toggle-row">
          <input type="checkbox" bind:checked={resume} />
          <span>Reprendre depuis le dernier checkpoint</span>
        </label>
      </div>

      {#if error}<p class="text-red mb-16">{error}</p>{/if}
      <button class="btn btn-primary w-full" style="padding:12px" onclick={submit} disabled={submitting || !project_id}>
        {submitting ? '▷ Lancement…' : '▷ Lancer le fine-tuning'}
      </button>
    </div>

    <!-- Summary panel -->
    <div class="train-summary">
      <div class="card" style="position:sticky;top:24px">
        <div class="card-title mb-16">Résumé</div>
        {#if project_id}
          {@const proj = projects.find(p => p.id === project_id)}
          {#if proj}
            <div class="summary-row"><span class="text-muted">Projet</span><span>{proj.name}</span></div>
            <div class="summary-row"><span class="text-muted">Tâche</span><span>{proj.domain}/{proj.task_type}</span></div>
          {/if}
        {/if}
        {#if selectedModel}
          <div class="summary-row"><span class="text-muted">Modèle</span><span style="font-size:12px">{selectedModel.name}</span></div>
        {/if}
        <div class="summary-row"><span class="text-muted">Iters</span><span>{iters}</span></div>
        <div class="summary-row"><span class="text-muted">Batch</span><span>{batch_size}</span></div>
        <div class="summary-row"><span class="text-muted">LR</span><span class="mono">{learning_rate}</span></div>
        <div class="summary-row"><span class="text-muted">Rank</span><span>{lora_rank}</span></div>
        <div class="summary-row"><span class="text-muted">RAG</span><span style="color:{use_rag ? 'var(--green)' : 'var(--text-muted)'}">{use_rag ? 'activé' : 'désactivé'}</span></div>
        <hr class="separator" />
        <div class="text-muted" style="font-size:12px">
          Temps estimé : {Math.round(iters * 0.8 / 60)} – {Math.round(iters * 1.2 / 60)} min
        </div>
      </div>
    </div>
  </div>
</div>

<style>
  .train-layout { display: grid; grid-template-columns: 1fr 300px; gap: 24px; align-items: start; }
  .model-cards { display: flex; flex-direction: column; gap: 10px; }
  .model-card {
    padding: 12px 16px; border: 2px solid var(--border); border-radius: var(--radius);
    cursor: pointer; transition: border-color 200ms, background 200ms;
  }
  .model-card:hover { background: var(--surface-2); }
  .model-card.selected { border-color: var(--accent); background: var(--accent-dim); }
  .model-name { font-weight: 600; margin-bottom: 2px; }
  .model-path { font-size: 11px; }
  .toggle-row { display: flex; align-items: center; gap: 10px; padding: 8px 0; cursor: pointer; font-size: 14px; }
  .toggle-row input { accent-color: var(--accent); width: 16px; height: 16px; }
  .summary-row { display: flex; justify-content: space-between; padding: 6px 0; border-bottom: 1px solid var(--border); font-size: 13px; }
  .summary-row:last-of-type { border-bottom: none; }
  .separator { border: none; border-top: 1px solid var(--border); margin: 12px 0; }
  @media (max-width: 900px) { .train-layout { grid-template-columns: 1fr; } .train-summary { display: none; } }
</style>
