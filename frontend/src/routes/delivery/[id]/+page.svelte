<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import { getPackageInfo } from '$lib/api';
  import type { PackageInfo } from '$lib/types';
  import StatusBadge from '$lib/components/StatusBadge.svelte';

  const id = $derived($page.params.id as string);
  let info: PackageInfo | null = $state(null);

  onMount(async () => {
    info = await getPackageInfo(id).catch(() => null);
  });

  function download(type: 'fused' | 'adapter') {
    window.location.href = `/api/models/${id}/download/${type}`;
  }
</script>

<div class="topbar">
  <span class="topbar-title">Livraison modèle <span class="mono text-accent">{id}</span></span>
</div>

<div class="content">
  {#if info}
    <!-- Header -->
    <div class="card mb-16">
      <div class="delivery-header">
        <div>
          <div style="font-size:20px;font-weight:700">{info.project_name}</div>
          <div class="text-muted">{info.model_name}</div>
        </div>
        <div class="text-right">
          <div class="text-muted" style="font-size:12px">Déployé le</div>
          <div>{info.deployed_at?.slice(0, 10)}</div>
          <div class="flex gap-8 mt-8 justify-end">
            {#if info.run.val_loss !== null}
              <span class="badge badge-completed">Val loss: {info.run.val_loss.toFixed(4)}</span>
            {/if}
          </div>
        </div>
      </div>
    </div>

    <!-- Package files -->
    <div class="grid-2 mb-16">
      <div class="card pkg-card">
        <div class="card-header">
          <div class="card-title">Modèle fusionné</div>
          {#if info.fused.exists}
            <span class="badge badge-completed">✓ disponible</span>
          {:else}
            <span class="badge badge-failed">✗ manquant</span>
          {/if}
        </div>
        <div class="pkg-path mono text-muted">{info.fused.path}</div>
        <div class="pkg-size">{info.fused.size_mb} MB</div>
        <button
          class="btn btn-primary w-full mt-16"
          onclick={() => download('fused')}
          disabled={!info.fused.exists}
        >
          ↓ Télécharger modèle fusionné (.zip)
        </button>
      </div>

      <div class="card pkg-card">
        <div class="card-header">
          <div class="card-title">Adapter LoRA</div>
          {#if info.adapter.exists}
            <span class="badge badge-completed">✓ disponible</span>
          {:else}
            <span class="badge badge-failed">✗ manquant</span>
          {/if}
        </div>
        <div class="pkg-path mono text-muted">{info.adapter.path}</div>
        <div class="pkg-size">{info.adapter.size_mb} MB</div>
        <button
          class="btn btn-ghost w-full mt-16"
          onclick={() => download('adapter')}
          disabled={!info.adapter.exists}
        >
          ↓ Télécharger adapter seul (.zip)
        </button>
      </div>
    </div>

    <!-- Evaluations -->
    {#if info.evaluations.length > 0}
      <div class="card mb-16">
        <div class="card-title mb-16">Métriques d'évaluation</div>
        <div class="grid-4">
          {#each info.evaluations.slice(0, 1) as e}
            <div class="metric">
              <div class="metric-label">BLEU</div>
              <div class="metric-value">{e.bleu?.toFixed(3) ?? '—'}</div>
            </div>
            <div class="metric">
              <div class="metric-label">Rouge-L</div>
              <div class="metric-value">{e.rouge_l?.toFixed(3) ?? '—'}</div>
            </div>
            <div class="metric">
              <div class="metric-label">Pass rate</div>
              <div class="metric-value" style="color:var(--green)">{e.pass_rate !== null ? `${(e.pass_rate*100).toFixed(1)}%` : '—'}</div>
            </div>
            <div class="metric">
              <div class="metric-label">Latence moy.</div>
              <div class="metric-value">{e.latency_ms ?? '—'} ms</div>
            </div>
          {/each}
        </div>
      </div>
    {/if}

    <!-- Run details -->
    <div class="card mb-16">
      <div class="card-title mb-12">Détails du run</div>
      <div class="grid-4">
        <div><div class="text-muted" style="font-size:12px">Iters</div><div class="mono">{info.run.iters}</div></div>
        <div><div class="text-muted" style="font-size:12px">LoRA rank</div><div class="mono">{info.run.lora_rank}</div></div>
        <div><div class="text-muted" style="font-size:12px">Learning rate</div><div class="mono">{info.run.learning_rate}</div></div>
        <div><div class="text-muted" style="font-size:12px">Val loss final</div><div class="mono text-accent">{info.run.val_loss?.toFixed(4) ?? '—'}</div></div>
      </div>
    </div>

    <!-- Changelog -->
    <div class="card">
      <div class="card-title mb-16">Historique des runs du projet</div>
      <table class="table">
        <thead>
          <tr><th>Run ID</th><th>Statut</th><th>Val loss</th><th>Train loss</th><th>Iters</th><th>Date</th></tr>
        </thead>
        <tbody>
          {#each info.changelog as run}
            <tr>
              <td class="mono text-accent">{run.id}</td>
              <td>{#if run.status}<span class="badge badge-{run.status}">{run.status}</span>{/if}</td>
              <td class="mono">{run.val_loss?.toFixed(4) ?? '—'}</td>
              <td class="mono">{run.train_loss?.toFixed(4) ?? '—'}</td>
              <td>{run.iters}</td>
              <td class="text-muted">{run.started_at?.slice(0,10)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <p class="text-muted">Chargement…</p>
  {/if}
</div>

<style>
  .delivery-header { display: flex; justify-content: space-between; align-items: flex-start; }
  .pkg-card { display: flex; flex-direction: column; }
  .pkg-path { font-size: 11px; margin-bottom: 4px; word-break: break-all; }
  .pkg-size { font-size: 24px; font-weight: 700; color: var(--accent); }
</style>
