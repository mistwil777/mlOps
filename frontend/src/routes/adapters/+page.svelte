<script lang="ts">
  import { onMount } from 'svelte';

  interface Adapter {
    id: string;
    project_id: string;
    name: string;
    domain: string;
    task_type: string;
    adapter_path: string;
    base_model: string;
    val_loss: number | null;
    rouge_l: number | null;
    pass_rate: number | null;
    n_train_pairs: number | null;
    avg_data_quality: number | null;
    status: string;
    created_at: string;
    notes: string | null;
  }

  interface DataLink {
    id: string;
    data_source: string;
    n_pairs: number;
    avg_quality: number | null;
    domain: string | null;
    task_type: string | null;
    prompt_token_p50: number | null;
    completion_token_p50: number | null;
  }

  let adapters: Adapter[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let selectedAdapter: Adapter | null = $state(null);
  let dataLinks: DataLink[] = $state([]);
  let linksLoading = $state(false);

  // Route tester
  let routeDomain = $state('code');
  let routeTask = $state('migration');
  let routeResult: Adapter | null = $state(null);
  let routeError = $state('');

  // Register form
  let showRegister = $state(false);
  let reg = $state({
    project_id: '', name: '', domain: 'code', task_type: 'migration',
    adapter_path: '', base_model: '', val_loss: '', rouge_l: '', notes: '',
  });
  let registering = $state(false);
  let registerError = $state('');

  onMount(async () => {
    await load();
  });

  async function load() {
    loading = true;
    try {
      const r = await fetch('/api/adapters');
      if (!r.ok) throw new Error(`${r.status}`);
      adapters = await r.json();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  async function selectAdapter(a: Adapter) {
    selectedAdapter = a;
    linksLoading = true;
    try {
      const r = await fetch(`/api/adapters/${a.id}/data-links`);
      dataLinks = r.ok ? await r.json() : [];
    } catch {
      dataLinks = [];
    } finally {
      linksLoading = false;
    }
  }

  async function handleDelete(id: string) {
    if (!confirm('Supprimer cet adaptateur ?')) return;
    await fetch(`/api/adapters/${id}`, { method: 'DELETE' });
    if (selectedAdapter?.id === id) { selectedAdapter = null; dataLinks = []; }
    await load();
  }

  async function handleRoute() {
    routeError = '';
    routeResult = null;
    try {
      const r = await fetch(`/api/adapters/route?domain=${encodeURIComponent(routeDomain)}&task_type=${encodeURIComponent(routeTask)}`);
      if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
      routeResult = await r.json();
    } catch (e) {
      routeError = (e as Error).message;
    }
  }

  async function handleRegister() {
    registerError = '';
    registering = true;
    try {
      const body = {
        ...reg,
        val_loss: reg.val_loss ? parseFloat(reg.val_loss) : null,
        rouge_l: reg.rouge_l ? parseFloat(reg.rouge_l) : null,
      };
      const r = await fetch('/api/adapters', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
      showRegister = false;
      await load();
    } catch (e) {
      registerError = (e as Error).message;
    } finally {
      registering = false;
    }
  }

  function scoreColor(val: number | null, better: 'lower' | 'higher' = 'higher') {
    if (val === null) return 'var(--text-muted)';
    if (better === 'lower') return val < 1.5 ? 'var(--green)' : val < 2.5 ? 'var(--accent)' : 'var(--red)';
    return val > 0.7 ? 'var(--green)' : val > 0.5 ? 'var(--accent)' : 'var(--red)';
  }
</script>

<div class="topbar">
  <span class="topbar-title">Adaptateurs LoRA</span>
  <div style="display:flex; gap:8px">
    <button class="btn btn-ghost" onclick={load} style="font-size:12px">↻</button>
    <button class="btn btn-primary" onclick={() => showRegister = !showRegister}>+ Enregistrer</button>
  </div>
</div>

<div class="content">
  {#if error}
    <div class="banner-error" style="margin-bottom:16px">{error}</div>
  {/if}

  <!-- Register form -->
  {#if showRegister}
    <div class="card" style="margin-bottom:20px; padding:20px">
      <div class="section-title">Enregistrer un adaptateur</div>
      <div class="reg-grid">
        {#each [
          ['project_id', 'Projet'],
          ['name', 'Nom'],
          ['domain', 'Domaine'],
          ['task_type', 'Task type'],
          ['adapter_path', 'Chemin adaptateur'],
          ['base_model', 'Modèle de base'],
          ['val_loss', 'Val loss'],
          ['rouge_l', 'ROUGE-L'],
        ] as [field, label]}
          <label class="form-field">
            <span class="field-label">{label}</span>
            <input bind:value={(reg as any)[field]} class="input" placeholder={label} />
          </label>
        {/each}
        <label class="form-field" style="grid-column: 1 / -1">
          <span class="field-label">Notes</span>
          <textarea bind:value={reg.notes} class="input" rows="2" style="resize:vertical"></textarea>
        </label>
      </div>
      <div style="margin-top:12px; display:flex; gap:8px">
        <button class="btn btn-primary" onclick={handleRegister} disabled={registering}>
          {registering ? '…' : 'Enregistrer'}
        </button>
        <button class="btn btn-ghost" onclick={() => showRegister = false}>Annuler</button>
      </div>
      {#if registerError}
        <div class="banner-error" style="margin-top:8px">{registerError}</div>
      {/if}
    </div>
  {/if}

  <!-- Route tester -->
  <div class="card" style="margin-bottom:20px; padding:16px">
    <div class="section-title">Routage automatique</div>
    <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap">
      <label class="form-field" style="flex-direction:row; align-items:center; gap:8px">
        <span class="field-label">Domaine</span>
        <input bind:value={routeDomain} class="input" style="width:120px" />
      </label>
      <label class="form-field" style="flex-direction:row; align-items:center; gap:8px">
        <span class="field-label">Task type</span>
        <input bind:value={routeTask} class="input" style="width:120px" />
      </label>
      <button class="btn btn-primary" onclick={handleRoute}>Trouver le meilleur adaptateur</button>
    </div>
    {#if routeError}
      <div class="text-muted" style="font-size:13px; margin-top:8px">Aucun adaptateur trouvé pour cette combinaison.</div>
    {/if}
    {#if routeResult}
      <div class="route-result" style="margin-top:12px">
        <strong>→ {routeResult.name}</strong>
        <span class="mono text-muted" style="font-size:12px">{routeResult.adapter_path}</span>
        {#if routeResult.val_loss !== null}
          <span class="badge badge-completed">val_loss: {routeResult.val_loss?.toFixed(3)}</span>
        {/if}
      </div>
    {/if}
  </div>

  <!-- Adapter list + detail -->
  <div class="adapter-layout">
    <div class="adapter-list">
      {#if loading}
        <p class="text-muted">Chargement…</p>
      {:else if adapters.length === 0}
        <p class="text-muted">Aucun adaptateur enregistré.</p>
      {:else}
        {#each adapters as a}
          <div
            class="adapter-card card {selectedAdapter?.id === a.id ? 'selected' : ''}"
            onclick={() => selectAdapter(a)}
            role="button"
            tabindex="0"
            onkeydown={(e) => e.key === 'Enter' && selectAdapter(a)}
          >
            <div class="adapter-header">
              <div>
                <div class="adapter-name">{a.name}</div>
                <div class="mono text-muted" style="font-size:11px">{a.domain} / {a.task_type}</div>
              </div>
              <div style="display:flex; gap:6px; align-items:center">
                <span class="badge badge-completed" style="font-size:11px">{a.status}</span>
                <button class="btn btn-ghost" style="font-size:11px; color:var(--red); padding:2px 6px" onclick={(e) => { e.stopPropagation(); handleDelete(a.id); }}>✕</button>
              </div>
            </div>
            <div class="adapter-scores">
              {#if a.val_loss !== null}
                <div class="score"><span class="score-label">val_loss</span><span class="mono" style="color:{scoreColor(a.val_loss, 'lower')}">{a.val_loss.toFixed(3)}</span></div>
              {/if}
              {#if a.rouge_l !== null}
                <div class="score"><span class="score-label">ROUGE-L</span><span class="mono" style="color:{scoreColor(a.rouge_l)}">{(a.rouge_l * 100).toFixed(1)}%</span></div>
              {/if}
              {#if a.n_train_pairs !== null}
                <div class="score"><span class="score-label">paires</span><span class="mono">{a.n_train_pairs}</span></div>
              {/if}
            </div>
            <div class="mono text-muted" style="font-size:10px">{a.project_id} · {a.created_at.slice(0,10)}</div>
          </div>
        {/each}
      {/if}
    </div>

    {#if selectedAdapter}
      <div class="adapter-detail card">
        <div class="section-title">{selectedAdapter.name}</div>
        <div class="detail-rows">
          <div class="detail-row"><span>Chemin</span><span class="mono">{selectedAdapter.adapter_path}</span></div>
          <div class="detail-row"><span>Modèle base</span><span class="mono">{selectedAdapter.base_model}</span></div>
          <div class="detail-row"><span>Domaine</span><span>{selectedAdapter.domain} / {selectedAdapter.task_type}</span></div>
          {#if selectedAdapter.notes}
            <div class="detail-row"><span>Notes</span><span>{selectedAdapter.notes}</span></div>
          {/if}
        </div>

        <div class="section-title" style="margin-top:16px">Lignage données → scores</div>
        {#if linksLoading}
          <p class="text-muted" style="font-size:12px">Chargement…</p>
        {:else if dataLinks.length === 0}
          <p class="text-muted" style="font-size:12px">Aucun lien de données enregistré pour cet adaptateur.</p>
        {:else}
          <table class="links-table">
            <thead><tr><th>Source</th><th>Paires</th><th>Qualité</th><th>Domaine</th><th>P50 prompt</th></tr></thead>
            <tbody>
              {#each dataLinks as lnk}
                <tr>
                  <td class="mono" style="font-size:11px">{lnk.data_source}</td>
                  <td class="mono">{lnk.n_pairs}</td>
                  <td class="mono">{lnk.avg_quality != null ? (lnk.avg_quality * 100).toFixed(1) + '%' : '—'}</td>
                  <td class="text-muted" style="font-size:11px">{lnk.domain ?? '—'}</td>
                  <td class="mono">{lnk.prompt_token_p50 ?? '—'}</td>
                </tr>
              {/each}
            </tbody>
          </table>
        {/if}
      </div>
    {/if}
  </div>
</div>

<style>
  .section-title { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }
  .reg-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
  .form-field { display: flex; flex-direction: column; gap: 5px; }
  .field-label { font-size: 12px; color: var(--text-muted); }
  .input { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 6px 10px; font-size: 13px; }
  .route-result { display: flex; align-items: center; gap: 10px; font-size: 13px; }
  .adapter-layout { display: grid; grid-template-columns: 320px 1fr; gap: 16px; }
  .adapter-list { display: flex; flex-direction: column; gap: 8px; }
  .adapter-card { padding: 12px; cursor: pointer; border: 1px solid transparent; transition: border-color 0.15s; }
  .adapter-card:hover { border-color: var(--accent); }
  .adapter-card.selected { border-color: var(--accent); background: rgba(130,110,255,0.05); }
  .adapter-header { display: flex; justify-content: space-between; margin-bottom: 8px; }
  .adapter-name { font-size: 14px; font-weight: 600; }
  .adapter-scores { display: flex; gap: 14px; margin-bottom: 6px; }
  .score { display: flex; flex-direction: column; gap: 2px; }
  .score-label { font-size: 10px; color: var(--text-muted); }
  .adapter-detail { padding: 20px; }
  .detail-rows { display: flex; flex-direction: column; gap: 8px; }
  .detail-row { display: flex; gap: 12px; font-size: 13px; }
  .detail-row span:first-child { color: var(--text-muted); width: 100px; flex-shrink: 0; }
  .links-table { width: 100%; border-collapse: collapse; font-size: 12px; }
  .links-table th { text-align: left; padding: 5px 10px; color: var(--text-muted); border-bottom: 1px solid var(--border); }
  .links-table td { padding: 5px 10px; border-bottom: 1px solid var(--surface-2); }
</style>
