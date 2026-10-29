<script lang="ts">
  import { onMount } from 'svelte';
  import WilsonCiBar from '$lib/components/WilsonCiBar.svelte';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import SectionTabs from '$lib/components/SectionTabs.svelte';
  import { req } from '$lib/api';

  const tabs = [{ href: '/eval', label: 'Tests' }, { href: '/evals', label: 'Dashboard KPIs' }, { href: '/playground', label: 'Playground' }];

  interface ProjectKpiSummary {
    project_id: string;
    project_name: string;
    sector: string | null;
    last_eval_at: string | null;
    pass_rate: number | null;
    pass_rate_ci_low: number | null;
    pass_rate_ci_high: number | null;
    rouge_l: number | null;
    latency_p95: number | null;
    delta_pass_rate: number | null;
    n_runs: number;
    quality_gate_pass: boolean | null;
  }

  let projects: ProjectKpiSummary[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let search = $state('');
  let sectorFilter = $state('all');

  const filtered = $derived(
    projects.filter(p => {
      const matchSearch = p.project_name.toLowerCase().includes(search.toLowerCase());
      const matchSector = sectorFilter === 'all' || p.sector === sectorFilter;
      return matchSearch && matchSector;
    })
  );

  const sectors = $derived(['all', ...new Set(projects.map(p => p.sector).filter((s): s is string => s != null))]);

  const stats = $derived({
    total: projects.length,
    passing: projects.filter(p => p.quality_gate_pass === true).length,
    regressing: projects.filter(p => p.delta_pass_rate != null && p.delta_pass_rate < -0.03).length,
    noData: projects.filter(p => p.last_eval_at == null).length,
  });

  function formatDate(iso: string | null): string {
    if (!iso) return '—';
    return new Date(iso).toLocaleDateString('fr-FR', { day: '2-digit', month: 'short' });
  }

  function deltaLabel(delta: number | null): string {
    if (delta == null) return '';
    const sign = delta >= 0 ? '+' : '';
    return `${sign}${(delta * 100).toFixed(1)}pp`;
  }

  function sectorLabel(s: string | null): string {
    const map: Record<string, string> = {
      defense: 'Défense', banking: 'Banque', legal: 'Juridique', generic: 'Générique',
    };
    return s ? (map[s] ?? s) : '—';
  }

  function sectorClass(s: string | null): string {
    const map: Record<string, string> = {
      defense: 'sector-defense', banking: 'sector-banking',
      legal: 'sector-legal', generic: 'sector-generic',
    };
    return s ? (map[s] ?? '') : '';
  }

  onMount(async () => {
    try {
      projects = await req<ProjectKpiSummary[]>('/kpis/dashboard?limit=50');
    } catch (e: any) {
      error = e.message ?? 'Erreur de chargement';
    } finally {
      loading = false;
    }
  });
</script>

<SectionTabs {tabs} />

<div class="page">
  <div class="page-header">
    <div>
      <h1>Dashboard KPIs</h1>
      <p class="subtitle">Suivi des évaluations sur {projects.length} projets</p>
    </div>
  </div>

  <!-- Stats globales -->
  <div class="stat-row">
    <div class="stat-card">
      <div class="stat-value">{stats.total}</div>
      <div class="stat-label">Projets actifs</div>
    </div>
    <div class="stat-card success">
      <div class="stat-value">{stats.passing}</div>
      <div class="stat-label">Gate passé</div>
    </div>
    <div class="stat-card warning">
      <div class="stat-value">{stats.regressing}</div>
      <div class="stat-label">En régression</div>
    </div>
    <div class="stat-card muted">
      <div class="stat-value">{stats.noData}</div>
      <div class="stat-label">Sans données</div>
    </div>
  </div>

  <!-- Filtres -->
  <div class="filter-row">
    <input
      class="search-input"
      type="search"
      placeholder="Rechercher un projet…"
      bind:value={search}
    />
    <div class="sector-tabs">
      {#each sectors as s}
        <button
          class="sector-tab {sectorFilter === s ? 'active' : ''}"
          onclick={() => sectorFilter = s}
        >
          {s === 'all' ? 'Tous' : sectorLabel(s)}
        </button>
      {/each}
    </div>
  </div>

  {#if loading}
    <div class="loading">Chargement…</div>
  {:else if error}
    <div class="error-msg">{error}</div>
  {:else}
    <div class="projects-table-wrap">
      <table class="projects-table">
        <thead>
          <tr>
            <th>Projet</th>
            <th>Secteur</th>
            <th>Pass rate (95% CI)</th>
            <th>ROUGE-L</th>
            <th>Latence p95</th>
            <th>Δ vs préc.</th>
            <th>Dernier eval</th>
            <th>Gate</th>
          </tr>
        </thead>
        <tbody>
          {#each filtered as p}
            <tr
              class="project-row"
              onclick={() => window.location.href = `/evals/${p.project_id}`}
              role="link"
              tabindex="0"
              onkeydown={(e) => e.key === 'Enter' && (window.location.href = `/evals/${p.project_id}`)}
            >
              <td class="project-name">
                <span class="name">{p.project_name}</span>
                <span class="proj-id">{p.project_id}</span>
              </td>
              <td><span class="sector-badge {sectorClass(p.sector)}">{sectorLabel(p.sector)}</span></td>
              <td>
                <WilsonCiBar
                  value={p.pass_rate}
                  ci_low={p.pass_rate_ci_low}
                  ci_high={p.pass_rate_ci_high}
                  threshold={0.7}
                />
              </td>
              <td class="metric-cell">
                {p.rouge_l != null ? (p.rouge_l * 100).toFixed(1) + '%' : '—'}
              </td>
              <td class="metric-cell">
                {p.latency_p95 != null ? p.latency_p95.toFixed(0) + ' ms' : '—'}
              </td>
              <td>
                {#if p.delta_pass_rate != null}
                  <span class="delta-badge {p.delta_pass_rate >= 0 ? 'up' : 'down'}">
                    {deltaLabel(p.delta_pass_rate)}
                  </span>
                {:else}
                  <span class="text-muted">—</span>
                {/if}
              </td>
              <td class="metric-cell">{formatDate(p.last_eval_at)}</td>
              <td>
                {#if p.quality_gate_pass === true}
                  <span class="gate pass">✓ Pass</span>
                {:else if p.quality_gate_pass === false}
                  <span class="gate fail">✗ Fail</span>
                {:else}
                  <span class="gate na">—</span>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
      {#if filtered.length === 0}
        <div class="empty-state">Aucun projet ne correspond aux filtres.</div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .page { padding: var(--sp-6) var(--sp-8); max-width: 1200px; }
  .page-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: var(--sp-6); }
  h1 { font-size: var(--text-2xl); font-weight: 700; color: var(--ink); margin-bottom: 4px; }
  .subtitle { font-size: var(--text-sm); color: var(--text-muted); }

  .stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: var(--sp-4); margin-bottom: var(--sp-6); }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    padding: var(--sp-4) var(--sp-5);
    border-top: 3px solid var(--border);
  }
  .stat-card.success { border-top-color: var(--green); }
  .stat-card.warning { border-top-color: var(--yellow); }
  .stat-card.muted   { border-top-color: var(--border); }
  .stat-value { font-size: 28px; font-weight: 700; color: var(--ink); }
  .stat-label { font-size: var(--text-xs); color: var(--text-muted); font-weight: 500; margin-top: 2px; text-transform: uppercase; letter-spacing: .05em; }

  .filter-row { display: flex; align-items: center; gap: var(--sp-4); margin-bottom: var(--sp-5); }
  .search-input {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 7px 12px;
    font-size: var(--text-sm);
    font-family: inherit;
    background: var(--surface);
    color: var(--ink);
    width: 260px;
    outline: none;
  }
  .search-input:focus { border-color: var(--accent); }
  .sector-tabs { display: flex; gap: 4px; }
  .sector-tab {
    padding: 5px 12px;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    font-size: var(--text-xs);
    font-weight: 600;
    background: var(--surface);
    color: var(--text-muted);
    cursor: pointer;
  }
  .sector-tab.active { background: var(--accent); color: white; border-color: var(--accent); }

  .projects-table-wrap { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; }
  .projects-table { width: 100%; border-collapse: collapse; }
  .projects-table thead th {
    background: var(--surface-3);
    padding: 10px 14px;
    text-align: left;
    font-size: 11px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .05em;
    color: var(--text-muted);
    border-bottom: 2px solid var(--border);
  }
  .projects-table td { padding: 12px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  .project-row { cursor: pointer; transition: background 0.1s; }
  .project-row:hover td { background: var(--surface-2); }
  .project-row:last-child td { border-bottom: none; }

  .project-name { min-width: 220px; }
  .name { font-weight: 600; color: var(--ink); display: block; font-size: var(--text-sm); }
  .proj-id { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }

  .sector-badge {
    font-size: 11px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 100px;
    background: var(--surface-3);
    color: var(--text-muted);
  }
  .sector-defense { background: #fef3c7; color: #92400e; }
  .sector-banking  { background: #dbeafe; color: #1d4ed8; }
  .sector-legal    { background: #ede9fe; color: #5b21b6; }
  .sector-generic  { background: var(--surface-3); color: var(--text-muted); }

  .metric-cell { font-size: var(--text-sm); color: var(--ink-2); font-variant-numeric: tabular-nums; }

  .delta-badge {
    display: inline-block;
    font-size: 12px;
    font-weight: 700;
    padding: 2px 7px;
    border-radius: 4px;
  }
  .delta-badge.up   { background: #d1fae5; color: #065f46; }
  .delta-badge.down { background: #fee2e2; color: #991b1b; }

  .gate { font-size: 12px; font-weight: 700; padding: 2px 8px; border-radius: 4px; }
  .gate.pass { background: #d1fae5; color: #065f46; }
  .gate.fail { background: #fee2e2; color: #991b1b; }
  .gate.na   { color: var(--text-muted); }

  .loading, .error-msg, .empty-state {
    padding: var(--sp-8);
    text-align: center;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
  .error-msg { color: var(--red); }
  .text-muted { color: var(--text-muted); }
</style>
