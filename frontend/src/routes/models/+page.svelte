<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getModels, serveModel, stopModel, deleteModel } from '$lib/api';
  import type { DeployedModel } from '$lib/types';
  import StatusBadge from '$lib/components/StatusBadge.svelte';

  let models: DeployedModel[] = $state([]);
  let selected: Set<string>   = $state(new Set());
  let loading = $state(true);

  onMount(async () => {
    models = await getModels().catch(() => []);
    loading = false;
  });

  async function toggleServe(m: DeployedModel) {
    if (m.status === 'serving') {
      await stopModel(m.id);
    } else {
      await serveModel(m.id);
    }
    models = await getModels();
  }

  async function remove(m: DeployedModel) {
    if (!confirm(`Supprimer le modèle ${m.id} ?`)) return;
    await deleteModel(m.id);
    models = models.filter(x => x.id !== m.id);
  }

  function toggleSelect(id: string) {
    const s = new Set(selected);
    s.has(id) ? s.delete(id) : s.add(id);
    selected = s;
  }

  const compareList = $derived(models.filter(m => selected.has(m.id)));

  type RowGetter = (m: DeployedModel) => string | number | null | undefined;
  const compareRows: [string, RowGetter][] = [
    ['Projet',  (m) => m.project_name ?? m.project_id],
    ['Modèle',  (m) => m.model_name ?? '—'],
    ['Val loss', (m) => m.val_loss?.toFixed(4) ?? '—'],
    ['Statut',  (m) => m.status],
    ['Port',    (m) => m.serving_port],
    ['Taille',  (m) => m.size_mb ? `${m.size_mb} MB` : '—'],
  ];
</script>

<div class="topbar">
  <span class="topbar-title">Catalogue de modèles</span>
  {#if selected.size >= 2}
    <button class="btn btn-ghost">⇌ Comparer ({selected.size})</button>
  {/if}
</div>

<div class="content">
  {#if loading}
    <p class="text-muted">Chargement…</p>
  {:else if models.length === 0}
    <div class="card" style="text-align:center;padding:48px;color:var(--text-muted)">
      Aucun modèle déployé.<br/><a href="/train">Lancer un training</a> pour créer le premier.
    </div>
  {:else}
    <div class="models-grid">
      {#each models as m}
        <div class="model-card card {selected.has(m.id) ? 'model-selected' : ''}">
          <div class="model-card-header">
            <input type="checkbox" onchange={() => toggleSelect(m.id)} checked={selected.has(m.id)} />
            <div style="flex:1;min-width:0">
              <div class="model-card-name">{m.project_name ?? m.project_id}</div>
              <div class="text-muted mono" style="font-size:11px">{m.model_name ?? '—'}</div>
            </div>
            <StatusBadge status={m.status} />
          </div>

          <div class="model-card-metrics">
            {#if m.val_loss !== null}<div class="kv"><span>Val loss</span><span class="mono text-accent">{m.val_loss.toFixed(4)}</span></div>{/if}
            {#if m.size_mb !== null}<div class="kv"><span>Taille</span><span>{m.size_mb} MB</span></div>{/if}
            <div class="kv"><span>Port</span><span>{m.serving_port}</span></div>
            <div class="kv"><span>Déployé</span><span>{m.deployed_at?.slice(0,10)}</span></div>
          </div>

          <div class="model-card-actions">
            <button
              class="btn {m.status === 'serving' ? 'btn-danger' : 'btn-primary'}"
              style="flex:1;padding:6px"
              onclick={() => toggleServe(m)}
            >
              {m.status === 'serving' ? '■ Stop' : '▷ Serve'}
            </button>
            <button class="btn btn-ghost" style="padding:6px 12px" onclick={() => goto(`/delivery/${m.id}`)}>
              ↓ Livrer
            </button>
            <button class="btn btn-ghost" style="padding:6px 10px;color:var(--red)" onclick={() => remove(m)}>
              ✕
            </button>
          </div>
        </div>
      {/each}
    </div>

    <!-- Comparison table -->
    {#if compareList.length >= 2}
      <div class="card mt-24">
        <div class="card-title mb-16">Comparaison</div>
        <table class="table">
          <thead>
            <tr>
              <th>Métrique</th>
              {#each compareList as m}<th>{m.id}</th>{/each}
            </tr>
          </thead>
          <tbody>
            {#each compareRows as [label, getter]}
              <tr>
                <td class="text-muted">{label}</td>
                {#each compareList as m}<td class="mono">{getter(m) ?? '—'}</td>{/each}
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {/if}
</div>

<style>
  .models-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; }
  .model-card { display: flex; flex-direction: column; gap: 12px; transition: border-color 200ms; }
  .model-selected { border-color: var(--accent); }
  .model-card-header { display: flex; align-items: flex-start; gap: 10px; }
  .model-card-name { font-weight: 600; font-size: 15px; }
  .model-card-metrics { display: flex; flex-direction: column; gap: 4px; }
  .kv { display: flex; justify-content: space-between; font-size: 12px; }
  .kv span:first-child { color: var(--text-muted); }
  .model-card-actions { display: flex; gap: 8px; }
</style>
