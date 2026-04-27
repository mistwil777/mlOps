<script lang="ts">
  import { onMount } from 'svelte';
  import { getProjects, getDataHealth } from '$lib/api';
  import type { Project, DataHealth } from '$lib/types';

  let projects: Project[] = $state([]);
  let selectedProject = $state('');
  let health: DataHealth | null = $state(null);
  let loading = $state(false);
  let error = $state('');

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) {
      selectedProject = projects[0].id;
      await loadHealth();
    }
  });

  async function loadHealth() {
    if (!selectedProject) return;
    loading = true;
    error = '';
    try {
      health = await getDataHealth(selectedProject);
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  function funnelPct(val: number, max: number) {
    return max > 0 ? Math.round((val / max) * 100) : 0;
  }
</script>

<div class="topbar">
  <span class="topbar-title">Santé des données</span>
  <select bind:value={selectedProject} onchange={loadHealth} class="project-select">
    {#each projects as p}
      <option value={p.id}>{p.name}</option>
    {/each}
  </select>
</div>

<div class="content">
  {#if error}
    <div class="banner-error">{error}</div>
  {:else if loading}
    <p class="text-muted">Chargement…</p>
  {:else if !health}
    <p class="text-muted">Sélectionnez un projet.</p>
  {:else}
    <!-- Alerts -->
    {#if health.alerts.length > 0}
      <div class="alerts">
        {#each health.alerts as alert}
          <div class="alert-item">⚠ {alert}</div>
        {/each}
      </div>
    {/if}

    <!-- Funnel -->
    <div class="card section">
      <div class="section-title">Entonnoir de données</div>
      <div class="funnel">
        {#each [
          { label: 'Brutes (raw)', key: 'raw', color: 'var(--text-muted)' },
          { label: 'Traitées (processed)', key: 'processed', color: 'var(--accent)' },
          { label: 'Fine-tuning ready', key: 'fine_tuning_ready', color: 'var(--green)' },
        ] as stage}
          {@const count = health.funnel[stage.key as keyof typeof health.funnel]}
          {@const pct = funnelPct(count, health.funnel.raw)}
          <div class="funnel-row">
            <div class="funnel-label">{stage.label}</div>
            <div class="funnel-bar-wrap">
              <div class="funnel-bar" style="width:{pct}%; background:{stage.color}"></div>
            </div>
            <div class="funnel-count mono">{count.toLocaleString()}</div>
          </div>
        {/each}
      </div>
    </div>

    <!-- Stage details -->
    <div class="stages-grid">
      {#each [
        { key: 'raw', label: 'Raw', icon: '▤' },
        { key: 'processed', label: 'Processed', icon: '▥' },
        { key: 'fine_tuning_ready', label: 'Fine-tuning ready', icon: '▦' },
      ] as s}
        {@const st = health.stages[s.key as keyof typeof health.stages]}
        <div class="card stage-card">
          <div class="stage-header">
            <span style="font-size:18px">{s.icon}</span>
            <span class="stage-label">{s.label}</span>
            <span class="badge {st.exists ? 'badge-completed' : 'badge-failed'}">{st.exists ? 'présent' : 'vide'}</span>
          </div>
          <div class="stage-stats">
            <div class="stat"><span class="stat-label">Paires</span><span class="mono">{st.count.toLocaleString()}</span></div>
            <div class="stat"><span class="stat-label">Taille</span><span class="mono">{st.size_mb} MB</span></div>
            {#if 'avg_quality' in st && st.avg_quality !== null}
              <div class="stat"><span class="stat-label">Qualité moy.</span><span class="mono">{(st.avg_quality * 100).toFixed(1)}%</span></div>
            {/if}
            {#if 'train' in st}
              <div class="stat"><span class="stat-label">Train</span><span class="mono">{(st as any).train}</span></div>
              <div class="stat"><span class="stat-label">Valid</span><span class="mono">{(st as any).valid}</span></div>
            {/if}
            {#if 'reviewed_count' in st}
              <div class="stat"><span class="stat-label">Revus</span><span class="mono">{(st as any).reviewed_count}</span></div>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .project-select {
    background: var(--surface-2); color: var(--text); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 4px 10px; font-size: 13px;
  }
  .alerts { display: flex; flex-direction: column; gap: 8px; margin-bottom: 20px; }
  .alert-item {
    padding: 10px 14px; background: rgba(255,180,0,0.1); border: 1px solid rgba(255,180,0,0.4);
    border-radius: var(--radius); font-size: 13px; color: #ffb400;
  }
  .section { padding: 20px; }
  .section-title { font-size: 13px; font-weight: 600; color: var(--text-muted); margin-bottom: 16px; text-transform: uppercase; letter-spacing: 0.05em; }
  .funnel { display: flex; flex-direction: column; gap: 12px; }
  .funnel-row { display: flex; align-items: center; gap: 12px; }
  .funnel-label { width: 180px; font-size: 13px; }
  .funnel-bar-wrap { flex: 1; background: var(--surface-2); border-radius: 4px; height: 20px; overflow: hidden; }
  .funnel-bar { height: 100%; border-radius: 4px; transition: width 0.4s; min-width: 2px; }
  .funnel-count { width: 80px; text-align: right; font-size: 13px; }
  .stages-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; }
  .stage-card { padding: 16px; display: flex; flex-direction: column; gap: 12px; }
  .stage-header { display: flex; align-items: center; gap: 8px; }
  .stage-label { font-weight: 600; flex: 1; }
  .stage-stats { display: flex; flex-direction: column; gap: 6px; }
  .stat { display: flex; justify-content: space-between; font-size: 13px; }
  .stat-label { color: var(--text-muted); }
  .badge-failed { background: rgba(255,80,80,0.15); color: var(--red); border: 1px solid var(--red); padding: 2px 8px; border-radius: 4px; font-size: 11px; }
</style>
