<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getRuns, deleteRun } from '$lib/api';
  import type { Run } from '$lib/types';
  import StatusBadge from '$lib/components/StatusBadge.svelte';

  let runs: Run[] = $state([]);
  let loading = $state(true);
  let filterProject = $state('');
  let filterStatus = $state('');

  const projects = $derived([...new Set(runs.map(r => r.project_id))].sort());

  const filtered = $derived(runs.filter(r =>
    (!filterProject || r.project_id === filterProject) &&
    (!filterStatus  || r.status === filterStatus)
  ));

  onMount(async () => {
    runs = await getRuns().catch(() => []);
    loading = false;
  });

  async function remove(r: Run, e: MouseEvent) {
    e.stopPropagation();
    if (!confirm(`Supprimer le run ${r.id} ?`)) return;
    await deleteRun(r.id);
    runs = runs.filter(x => x.id !== r.id);
  }

  function fmt(s: number | null): string {
    if (!s) return '—';
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    return `${m}m ${s % 60}s`;
  }
</script>

<div class="topbar">
  <span class="topbar-title">Training runs</span>
  <span style="flex:1"></span>
  <button class="btn btn-primary" onclick={() => goto('/train')}>+ Nouveau run</button>
</div>

<div class="content">
  <div class="filters mb-16">
    <select bind:value={filterProject} class="select">
      <option value="">Tous les projets</option>
      {#each projects as p}<option value={p}>{p}</option>{/each}
    </select>
    <select bind:value={filterStatus} class="select">
      <option value="">Tous les statuts</option>
      <option value="running">running</option>
      <option value="completed">completed</option>
      <option value="failed">failed</option>
      <option value="cancelled">cancelled</option>
    </select>
  </div>

  {#if loading}
    <p class="text-muted">Chargement…</p>
  {:else if filtered.length === 0}
    <div class="card" style="text-align:center;padding:48px;color:var(--text-muted)">
      Aucun run trouvé.<br/><a href="/train">Lancer un training</a> pour commencer.
    </div>
  {:else}
    <table class="table">
      <thead>
        <tr>
          <th>ID</th>
          <th>Projet</th>
          <th>Modèle</th>
          <th>Statut</th>
          <th>Train loss</th>
          <th>Val loss</th>
          <th>Durée</th>
          <th>Démarré</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each filtered as r}
          <tr class="run-row" onclick={() => goto(`/runs/${r.id}`)}>
            <td class="mono text-accent" style="font-size:11px">{r.id.slice(0, 8)}…</td>
            <td>{r.project_id}</td>
            <td class="text-muted" style="font-size:12px">{r.model_name}</td>
            <td><StatusBadge status={r.status} /></td>
            <td class="mono">{r.train_loss?.toFixed(4) ?? '—'}</td>
            <td class="mono">{r.val_loss?.toFixed(4) ?? '—'}</td>
            <td class="text-muted">{fmt(r.duration_s)}</td>
            <td class="text-muted" style="font-size:11px">{r.started_at?.slice(0, 16).replace('T', ' ')}</td>
            <td>
              <button
                class="btn btn-ghost"
                style="padding:4px 8px;color:var(--red);font-size:12px"
                onclick={(e) => remove(r, e)}
              >✕</button>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
</div>

<style>
  .filters { display: flex; gap: 12px; }
  .select {
    background: var(--card-bg);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 6px 10px;
    border-radius: 6px;
    font-size: 13px;
  }
  .run-row { cursor: pointer; }
  .run-row:hover td { background: var(--hover-bg, rgba(255,255,255,0.03)); }
</style>
