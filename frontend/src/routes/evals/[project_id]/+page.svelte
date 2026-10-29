<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import WilsonCiBar from '$lib/components/WilsonCiBar.svelte';
  import KpiChart from '$lib/components/KpiChart.svelte';
  import KpiCompareTable, { type KpiColumnDef } from '$lib/components/KpiCompareTable.svelte';
  import CategoryBreakdown from '$lib/components/CategoryBreakdown.svelte';
  import { req } from '$lib/api';

  const projectId = $derived($page.params.project_id);

  let projectData: any = $state(null);
  let history: any[] = $state([]);
  let learningCurve: any[] = $state([]);
  let categoryItems: any[] = $state([]);
  let loading = $state(true);
  let error = $state('');
  let activeTab = $state(0);
  let compareRunIds = $state<string[]>([]);
  let compareData: any[] = $state([]);

  const tabs = ['Vue d\'ensemble', 'Évolution', 'Comparaison', 'Sécurité & Conformité', 'Runs bruts'];

  const COMPARE_METRICS: KpiColumnDef[] = [
    { key: 'pass_rate',         label: 'Pass rate',   format: 'pct', higher_is_better: true },
    { key: 'rouge_l',           label: 'ROUGE-L',     format: 'pct', higher_is_better: true },
    { key: 'exact_match',       label: 'Exact match', format: 'pct', higher_is_better: true },
    { key: 'exec_rate',         label: 'Exec rate',   format: 'pct', higher_is_better: true },
    { key: 'latency_p50',       label: 'Latence p50', format: 'ms',  higher_is_better: false },
    { key: 'latency_p95',       label: 'Latence p95', format: 'ms',  higher_is_better: false },
    { key: 'latency_p99',       label: 'Latence p99', format: 'ms',  higher_is_better: false },
    { key: 'tokens_per_second', label: 'Tokens/s',    format: 'tps', higher_is_better: true },
    { key: 'adapter_size_mb',   label: 'Adapter (Mo)', format: 'Mo',  higher_is_better: false },
  ];

  function fmtDate(iso: string | null): string {
    if (!iso) return '—';
    return new Date(iso).toLocaleDateString('fr-FR', { day: '2-digit', month: 'short', year: '2-digit' });
  }

  async function loadCompare() {
    if (compareRunIds.length < 2) return;
    try {
      compareData = await req<any[]>(`/kpis/projects/${projectId}/compare?run_ids=${compareRunIds.join(',')}`);
    } catch {}
  }

  onMount(async () => {
    try {
      const [pd, hist, lc] = await Promise.all([
        req<any>(`/kpis/projects/${projectId}`),
        req<any[]>(`/kpis/projects/${projectId}/history?limit=20`),
        req<any[]>(`/kpis/projects/${projectId}/learning-curve`),
      ]);
      projectData = pd;
      history = hist;
      learningCurve = lc;

      // Category breakdown for latest run
      if (pd?.latest_eval) {
        const latestRunId = pd.latest_eval.run_id ?? pd.latest_eval.id;
        if (latestRunId) {
          try {
            categoryItems = await req<any[]>(`/kpis/runs/${latestRunId}/category-breakdown`);
          } catch {}
        }
      }

      // Default compare: last 2 runs
      const runs = pd.history ?? [];
      if (runs.length >= 2) {
        compareRunIds = [runs[runs.length - 1].run_id, runs[runs.length - 2].run_id];
        await loadCompare();
      }
    } catch (e: any) {
      error = e.message ?? 'Erreur';
    } finally {
      loading = false;
    }
  });

  const latest = $derived(projectData?.latest_eval);

  const chartSeries = $derived([
    {
      label: 'Pass rate',
      data: history.map(h => h.pass_rate),
      color: '#10B981',
      fill: true,
    },
    {
      label: 'ROUGE-L',
      data: history.map(h => h.rouge_l),
      color: '#0284C7',
      dashed: true,
    },
  ]);

  const chartLabels = $derived(history.map(h => h.model_version ?? fmtDate(h.created_at)));

  const lcSeries = $derived([{
    label: 'Pass rate',
    data: learningCurve.map(p => p.pass_rate),
    color: '#10B981',
    fill: true,
  }]);
  const lcLabels = $derived(learningCurve.map(p => `${p.n_pairs} paires`));

  const compareColumns = $derived(
    compareData.map(r => ({
      run_id: r.run_id,
      version_tag: r.model_version ?? r.run_id.slice(-6),
      kpis: r,
    }))
  );

</script>

<div class="page">
  <div class="breadcrumb">
    <a href="/evals">← Dashboard KPIs</a>
  </div>

  {#if loading}
    <div class="loading">Chargement…</div>
  {:else if error}
    <div class="error-msg">{error}</div>
  {:else if projectData}
    <div class="page-header">
      <div>
        <h1>{projectData.project_name}</h1>
        <span class="proj-id">{projectId}</span>
      </div>
      {#if latest}
        <div class="header-kpis">
          <WilsonCiBar
            value={latest.pass_rate}
            ci_low={latest.pass_rate_ci_low}
            ci_high={latest.pass_rate_ci_high}
            label="Pass rate"
          />
          <div class="header-metric">
            <span class="metric-label">Latence p95</span>
            <span class="metric-value">{latest.latency_p95?.toFixed(0) ?? '—'} ms</span>
          </div>
          <div class="header-metric">
            <span class="metric-label">N évals</span>
            <span class="metric-value">{projectData.history?.length ?? 0}</span>
          </div>
        </div>
      {/if}
    </div>

    <!-- Tabs -->
    <div class="tabs">
      {#each tabs as tab, i}
        <button class="tab {activeTab === i ? 'active' : ''}" onclick={() => activeTab = i}>
          {tab}
        </button>
      {/each}
    </div>

    <!-- ── Tab 0 : Vue d'ensemble ─────────────────────────────────── -->
    {#if activeTab === 0}
      <div class="tab-content">
        {#if latest}
          <div class="dim-grid">

            <!-- Qualité fonctionnelle -->
            <div class="dim-card">
              <div class="dim-header quality">
                <span class="dim-icon">📊</span>
                <span class="dim-title">Qualité fonctionnelle</span>
              </div>
              <div class="dim-body">
                <div class="kpi-row">
                  <span class="kpi-name">Pass rate</span>
                  <WilsonCiBar value={latest.pass_rate} ci_low={latest.pass_rate_ci_low} ci_high={latest.pass_rate_ci_high} />
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">ROUGE-L</span>
                  <span class="kpi-val">{latest.rouge_l != null ? (latest.rouge_l * 100).toFixed(1) + '%' : '—'}</span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">Exact match</span>
                  <span class="kpi-val">{latest.exact_match != null ? (latest.exact_match * 100).toFixed(1) + '%' : '—'}</span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">Exec rate</span>
                  <span class="kpi-val">{latest.exec_rate != null ? (latest.exec_rate * 100).toFixed(1) + '%' : '—'}</span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">N échantillons</span>
                  <span class="kpi-val">{latest.n_samples ?? '—'}
                    {#if latest.sample_size_sufficient === false}
                      <span class="warn-badge">⚠ insuffisant</span>
                    {/if}
                  </span>
                </div>
              </div>
            </div>

            <!-- Performance -->
            <div class="dim-card">
              <div class="dim-header perf">
                <span class="dim-icon">⚡</span>
                <span class="dim-title">Performance</span>
              </div>
              <div class="dim-body">
                <div class="kpi-row">
                  <span class="kpi-name">Latence p50</span>
                  <span class="kpi-val">{latest.latency_p50?.toFixed(0) ?? '—'} ms</span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">Latence p95</span>
                  <span class="kpi-val">{latest.latency_p95?.toFixed(0) ?? '—'} ms</span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">Latence p99</span>
                  <span class="kpi-val">{latest.latency_p99?.toFixed(0) ?? '—'} ms</span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">Tokens/s</span>
                  <span class="kpi-val">{latest.tokens_per_second?.toFixed(1) ?? '—'}</span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">TTFT</span>
                  <span class="kpi-val">{latest.ttft_ms?.toFixed(0) ?? '—'} ms</span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">VRAM peak</span>
                  <span class="kpi-val">{latest.vram_peak_gb?.toFixed(1) ?? '—'} Go</span>
                </div>
              </div>
            </div>

            <!-- Évolution -->
            <div class="dim-card">
              <div class="dim-header evolution">
                <span class="dim-icon">📈</span>
                <span class="dim-title">Évolution</span>
              </div>
              <div class="dim-body">
                <div class="kpi-row">
                  <span class="kpi-name">Δ pass rate</span>
                  <span class="kpi-val {latest.delta_pass_rate != null ? (latest.delta_pass_rate >= 0 ? 'positive' : 'negative') : ''}">
                    {latest.delta_pass_rate != null ? (latest.delta_pass_rate >= 0 ? '+' : '') + (latest.delta_pass_rate * 100).toFixed(1) + 'pp' : '—'}
                  </span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">Régression rate</span>
                  <span class="kpi-val">{latest.regression_rate != null ? (latest.regression_rate * 100).toFixed(1) + '%' : '—'}</span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">Taille adapter</span>
                  <span class="kpi-val">{latest.adapter_size_mb?.toFixed(0) ?? '—'} Mo</span>
                </div>
                <div class="kpi-row">
                  <span class="kpi-name">Méthode éval</span>
                  <span class="kpi-val method-badge">{latest.evaluator_method ?? '—'}</span>
                </div>
              </div>
            </div>

          </div>

          <!-- Category breakdown -->
          {#if categoryItems.length > 0}
            <div class="section-card">
              <div class="section-title">Pass rate par catégorie</div>
              <CategoryBreakdown items={categoryItems} />
            </div>
          {/if}
        {:else}
          <div class="empty-state">Aucune évaluation disponible pour ce projet.</div>
        {/if}
      </div>

    <!-- ── Tab 1 : Évolution ───────────────────────────────────────── -->
    {:else if activeTab === 1}
      <div class="tab-content">
        {#if history.length > 1}
          <div class="section-card">
            <div class="section-title">Pass rate & ROUGE-L au fil des versions</div>
            <KpiChart
              series={chartSeries}
              labels={chartLabels}
              thresholdLine={0.7}
              height={260}
            />
          </div>
          {#if learningCurve.length > 0}
            <div class="section-card">
              <div class="section-title">Learning curve — pass rate en fonction du volume de données</div>
              <KpiChart
                series={lcSeries}
                labels={lcLabels}
                height={220}
                yLabel="Pass rate"
              />
            </div>
          {/if}
        {:else}
          <div class="empty-state">Minimum 2 versions nécessaires pour afficher l'évolution.</div>
        {/if}
      </div>

    <!-- ── Tab 2 : Comparaison ────────────────────────────────────── -->
    {:else if activeTab === 2}
      <div class="tab-content">
        <div class="section-card">
          <div class="section-title">Comparaison de versions</div>
          <KpiCompareTable runs={compareColumns} metrics={COMPARE_METRICS} />
        </div>
      </div>

    <!-- ── Tab 3 : Sécurité & Conformité ─────────────────────────── -->
    {:else if activeTab === 3}
      <div class="tab-content">
        <div class="pending-card">
          <div class="pending-icon">🔒</div>
          <div class="pending-title">Évaluation sécurité & conformité</div>
          <p>Lance un run d'évaluation avancée depuis la page détail du run correspondant.</p>
          <p class="pending-detail">Couvre : hallucination rate, mémorisation, robustesse adversariale, conformité format, traceabilité audit.</p>
        </div>
      </div>

    <!-- ── Tab 4 : Runs bruts ─────────────────────────────────────── -->
    {:else if activeTab === 4}
      <div class="tab-content">
        <div class="section-card" style="padding: 0; overflow: hidden;">
          <table class="runs-table">
            <thead>
              <tr>
                <th>Version</th>
                <th>Date</th>
                <th>Pass rate</th>
                <th>ROUGE-L</th>
                <th>p95</th>
                <th>N</th>
                <th>Méthode</th>
                <th>Détail</th>
              </tr>
            </thead>
            <tbody>
              {#each (projectData.history ?? []) as h}
                <tr>
                  <td><span class="version-chip">{h.model_version ?? '—'}</span></td>
                  <td class="mono-cell">{fmtDate(h.created_at)}</td>
                  <td><WilsonCiBar value={h.pass_rate} ci_low={h.pass_rate_ci_low} ci_high={h.pass_rate_ci_high} showCi={false} /></td>
                  <td class="mono-cell">{h.rouge_l != null ? (h.rouge_l * 100).toFixed(1) + '%' : '—'}</td>
                  <td class="mono-cell">{h.latency_p95?.toFixed(0) ?? '—'} ms</td>
                  <td class="mono-cell">{h.n_samples ?? '—'}</td>
                  <td><span class="method-badge">{h.evaluator_method ?? '—'}</span></td>
                  <td>
                    <a class="detail-link" href="/evals/{projectId}/{h.run_id}">→ Détail</a>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/if}
  {/if}
</div>

<style>
  .page { padding: var(--sp-6) var(--sp-8); max-width: 1100px; }
  .breadcrumb { margin-bottom: var(--sp-4); }
  .breadcrumb a { font-size: var(--text-sm); color: var(--accent); text-decoration: none; }
  .breadcrumb a:hover { text-decoration: underline; }

  .page-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: var(--sp-6); flex-wrap: wrap; gap: var(--sp-4); }
  h1 { font-size: var(--text-2xl); font-weight: 700; color: var(--ink); margin-bottom: 4px; }
  .proj-id { font-size: 12px; color: var(--text-muted); font-family: var(--font-mono); }

  .header-kpis { display: flex; gap: var(--sp-6); align-items: center; }
  .header-metric { display: flex; flex-direction: column; gap: 2px; }
  .metric-label { font-size: 11px; color: var(--text-muted); font-weight: 600; text-transform: uppercase; letter-spacing: .06em; }
  .metric-value { font-size: var(--text-sm); font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums; }

  .tabs { display: flex; border-bottom: 2px solid var(--border); margin-bottom: var(--sp-6); gap: 2px; }
  .tab {
    padding: 8px 16px;
    font-size: var(--text-sm);
    font-weight: 500;
    color: var(--text-muted);
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -2px;
    cursor: pointer;
  }
  .tab.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
  .tab:hover:not(.active) { color: var(--ink); }

  .tab-content { display: flex; flex-direction: column; gap: var(--sp-5); }

  .dim-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--sp-4); }
  .dim-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; }
  .dim-header {
    display: flex; align-items: center; gap: 8px;
    padding: 12px 16px;
    font-size: var(--text-sm); font-weight: 600; color: var(--ink);
    background: var(--surface-3);
    border-bottom: 1px solid var(--border);
  }
  .dim-header.quality  { border-left: 3px solid var(--green); }
  .dim-header.perf     { border-left: 3px solid #f59e0b; }
  .dim-header.evolution{ border-left: 3px solid var(--accent); }
  .dim-icon { font-size: 16px; }
  .dim-title { font-size: var(--text-sm); font-weight: 600; }
  .dim-body { padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; }

  .kpi-row { display: flex; align-items: center; justify-content: space-between; }
  .kpi-name { font-size: var(--text-xs); color: var(--text-muted); font-weight: 500; }
  .kpi-val { font-size: var(--text-sm); font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums; }
  .kpi-val.positive { color: var(--green); }
  .kpi-val.negative { color: var(--red); }

  .warn-badge { font-size: 10px; background: #fef3c7; color: #92400e; padding: 1px 5px; border-radius: 3px; margin-left: 4px; }
  .method-badge { font-size: 11px; background: #ede9fe; color: #5b21b6; padding: 1px 6px; border-radius: 4px; }

  .section-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: var(--sp-5); }
  .section-title { font-size: var(--text-sm); font-weight: 600; color: var(--ink); margin-bottom: var(--sp-4); }

  .pending-card {
    background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
    padding: var(--sp-8); text-align: center; max-width: 480px; margin: 0 auto;
  }
  .pending-icon { font-size: 36px; margin-bottom: var(--sp-3); }
  .pending-title { font-size: var(--text-base); font-weight: 600; margin-bottom: var(--sp-2); }
  .pending-detail { font-size: var(--text-xs); color: var(--text-muted); margin-top: var(--sp-2); }

  .runs-table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
  .runs-table thead th {
    background: var(--surface-3); padding: 10px 14px;
    font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .05em;
    color: var(--text-muted); border-bottom: 2px solid var(--border); text-align: left;
  }
  .runs-table td { padding: 10px 14px; border-bottom: 1px solid var(--border); vertical-align: middle; }
  .runs-table tr:last-child td { border-bottom: none; }
  .version-chip { font-size: 12px; font-weight: 700; background: #eff6ff; color: #1d4ed8; padding: 2px 7px; border-radius: 4px; }
  .mono-cell { font-family: var(--font-mono); font-size: 12px; font-variant-numeric: tabular-nums; color: var(--ink-2); }
  .detail-link { font-size: var(--text-xs); color: var(--accent); text-decoration: none; font-weight: 600; }

  .loading, .error-msg, .empty-state {
    padding: var(--sp-8); text-align: center; font-size: var(--text-sm); color: var(--text-muted);
  }
  .error-msg { color: var(--red); }

  @media (max-width: 900px) {
    .dim-grid { grid-template-columns: 1fr; }
  }
</style>
