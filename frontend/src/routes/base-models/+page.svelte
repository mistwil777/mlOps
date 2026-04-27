<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getBaseModels, validateModel, startDownload, getDownloadStatus } from '$lib/api';
  import type { BaseModel } from '$lib/types';

  let models: BaseModel[] = $state([]);
  let loading = $state(true);
  let validating: Record<string, boolean> = $state({});
  let validationResult: Record<string, { valid: boolean; error: string | null }> = $state({});
  let polling: ReturnType<typeof setInterval> | null = null;

  async function refresh() {
    models = await getBaseModels().catch(() => []);
    loading = false;
  }

  onMount(async () => {
    await refresh();
    // Poll every 3s while any download is in progress
    polling = setInterval(async () => {
      if (models.some(m => m.download_status === 'downloading')) {
        await refresh();
      }
    }, 3000);
  });

  onDestroy(() => { if (polling) clearInterval(polling); });

  async function handleValidate(m: BaseModel) {
    validating = { ...validating, [m.id]: true };
    validationResult = { ...validationResult, [m.id]: { valid: false, error: null } };
    try {
      const res = await validateModel(m.id);
      validationResult = { ...validationResult, [m.id]: res };
    } catch (e) {
      validationResult = { ...validationResult, [m.id]: { valid: false, error: (e as Error).message } };
    } finally {
      validating = { ...validating, [m.id]: false };
    }
  }

  async function handleDownload(m: BaseModel) {
    await startDownload(m.id);
    await refresh();
  }

  function statusColor(m: BaseModel): string {
    if (m.downloaded) return 'var(--green)';
    if (m.download_status === 'downloading') return 'var(--accent)';
    if (m.download_status === 'failed') return 'var(--red)';
    return 'var(--text-muted)';
  }

  function statusLabel(m: BaseModel): string {
    if (m.downloaded) return 'Disponible';
    if (m.download_status === 'downloading') return 'Téléchargement…';
    if (m.download_status === 'failed') return 'Échec';
    return 'Non téléchargé';
  }
</script>

<div class="topbar">
  <span class="topbar-title">Modèles de base</span>
  <span class="text-muted" style="font-size:12px">Source : HuggingFace mlx-community · Licence Apache 2.0</span>
</div>

<div class="content">
  {#if loading}
    <p class="text-muted">Chargement…</p>
  {:else}
    <div class="models-list">
      {#each models as m}
        {@const vr = validationResult[m.id]}
        <div class="model-card card">
          <!-- Header -->
          <div class="model-header">
            <div class="model-info">
              <div class="model-name">{m.name}</div>
              <div class="model-hf mono text-muted">{m.hf_path}</div>
            </div>
            <div class="model-status" style="color:{statusColor(m)}">
              {#if m.download_status === 'downloading'}
                <span class="spinner">↻</span>
              {/if}
              {statusLabel(m)}
            </div>
          </div>

          <!-- Characteristics -->
          <div class="model-specs">
            <div class="spec"><span class="spec-label">Format</span><span class="badge badge-completed">{m.format}</span></div>
            <div class="spec"><span class="spec-label">VRAM</span><span class="spec-val">{m.vram_gb} GB</span></div>
            <div class="spec"><span class="spec-label">Batch (train)</span><span class="spec-val">{m.training?.batch_size ?? '—'}</span></div>
            <div class="spec"><span class="spec-label">LoRA rank</span><span class="spec-val">{m.training?.rank ?? '—'}</span></div>
            <div class="spec"><span class="spec-label">Iters défaut</span><span class="spec-val">{m.training?.iters ?? '—'}</span></div>
            <div class="spec"><span class="spec-label">Max tokens</span><span class="spec-val">{m.inference?.max_tokens ?? '—'}</span></div>
          </div>

          {#if m.downloaded && m.local_path}
            <div class="local-path mono text-muted">{m.local_path}</div>
          {/if}

          <!-- Validation result -->
          {#if vr}
            <div class="validation-result {vr.valid ? 'ok' : 'err'}">
              {#if vr.valid}
                ✓ Repo valide — licence ouverte, poids MLX détectés
              {:else}
                ✗ {vr.error}
              {/if}
            </div>
          {/if}

          <!-- Actions -->
          <div class="model-actions">
            {#if !m.downloaded}
              <button
                class="btn btn-ghost"
                onclick={() => handleValidate(m)}
                disabled={validating[m.id]}
              >
                {validating[m.id] ? '…' : '✓ Valider'}
              </button>
              <button
                class="btn btn-primary"
                onclick={() => handleDownload(m)}
                disabled={m.download_status === 'downloading'}
              >
                {m.download_status === 'downloading' ? 'Téléchargement…' : '↓ Télécharger'}
              </button>
            {:else}
              <span class="badge badge-completed" style="padding:6px 12px">✓ Prêt pour fine-tuning</span>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .models-list { display: flex; flex-direction: column; gap: 16px; }
  .model-card { display: flex; flex-direction: column; gap: 12px; }
  .model-header { display: flex; justify-content: space-between; align-items: flex-start; }
  .model-name { font-size: 16px; font-weight: 700; }
  .model-hf { font-size: 12px; margin-top: 2px; }
  .model-status { font-size: 13px; font-weight: 600; white-space: nowrap; }
  .spinner { display: inline-block; animation: spin 1s linear infinite; margin-right: 4px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .model-specs { display: flex; flex-wrap: wrap; gap: 10px; }
  .spec { display: flex; align-items: center; gap: 6px; font-size: 12px; }
  .spec-label { color: var(--text-muted); }
  .spec-val { font-family: var(--font-mono); }
  .local-path { font-size: 11px; padding: 4px 8px; background: var(--surface-2); border-radius: 4px; }
  .validation-result { font-size: 12px; padding: 8px 12px; border-radius: var(--radius); }
  .validation-result.ok { background: rgba(80,200,120,0.1); color: var(--green); border: 1px solid var(--green); }
  .validation-result.err { background: rgba(255,80,80,0.1); color: var(--red); border: 1px solid var(--red); }
  .model-actions { display: flex; gap: 8px; align-items: center; }
</style>
