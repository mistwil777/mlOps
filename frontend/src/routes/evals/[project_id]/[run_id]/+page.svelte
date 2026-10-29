<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import WilsonCiBar from '$lib/components/WilsonCiBar.svelte';
  import CategoryBreakdown from '$lib/components/CategoryBreakdown.svelte';
  import { req } from '$lib/api';

  const projectId = $derived($page.params.project_id);
  const runId     = $derived($page.params.run_id);

  let data: any     = $state(null);
  let loading       = $state(true);
  let error         = $state('');

  onMount(async () => {
    try {
      data = await req<any>(`/kpis/runs/${runId}/full`);
    } catch (e: any) {
      error = e.message ?? 'Erreur';
    } finally {
      loading = false;
    }
  });

  const fn  = $derived(data?.functional ?? {});
  const rob = $derived(data?.robustness  ?? null);
  const sec = $derived(data?.security    ?? null);
  const cmp = $derived(data?.compliance  ?? null);
  const cat = $derived(data?.category_breakdown ?? []);

  function pct(v: number | null | undefined, dec = 1): string {
    return v != null ? (v * 100).toFixed(dec) + '%' : '—';
  }
  function ms(v: number | null | undefined): string {
    return v != null ? v.toFixed(0) + ' ms' : '—';
  }
  function score(v: number | null | undefined): string {
    return v != null ? v.toFixed(3) : '—';
  }
  function raw(v: number | null | undefined, unit = ''): string {
    return v != null ? v.toFixed(1) + (unit ? ' ' + unit : '') : '—';
  }

  type Row = { label: string; value: string; highlight?: boolean };

  function deltaClass(v: number | null | undefined, good_high = true): string {
    if (v == null) return '';
    return (good_high ? v >= 0.7 : v <= 0.1) ? 'good' : v > 0.4 ? 'warn' : 'bad';
  }
</script>

<div class="page">
  <div class="breadcrumb">
    <a href="/evals">Dashboard KPIs</a>
    <span class="sep">›</span>
    <a href="/evals/{projectId}">{projectId}</a>
    <span class="sep">›</span>
    <span>{runId}</span>
  </div>

  {#if loading}
    <div class="loading">Chargement…</div>
  {:else if error}
    <div class="error-msg">{error}</div>
  {:else if data}

    <div class="run-header">
      <div>
        <h1 class="run-id">{runId}</h1>
        <span class="proj-ref">{projectId}</span>
      </div>
      {#if fn.pass_rate != null}
        <WilsonCiBar
          value={fn.pass_rate}
          ci_low={fn.pass_rate_ci_low}
          ci_high={fn.pass_rate_ci_high}
          label="Pass rate"
        />
      {/if}
    </div>

    <div class="dim-grid">

      <!-- ── Qualité fonctionnelle ─────── -->
      <div class="dim-card">
        <div class="dim-head quality">
          <span>Qualité fonctionnelle</span>
          {#if fn.sample_size_sufficient === false}
            <span class="badge warn-badge">⚠ N insuffisant</span>
          {/if}
        </div>
        <table class="kpi-table">
          <tbody>
            <tr><td>Pass rate</td><td class="{deltaClass(fn.pass_rate)}">{pct(fn.pass_rate)}</td></tr>
            <tr><td>IC 95%</td><td>[{pct(fn.pass_rate_ci_low, 0)} – {pct(fn.pass_rate_ci_high, 0)}]</td></tr>
            <tr><td>Marge IC</td><td>{fn.pass_rate_ci_margin != null ? '±' + pct(fn.pass_rate_ci_margin, 1) : '—'}</td></tr>
            <tr><td>ROUGE-L</td><td>{pct(fn.rouge_l)}</td></tr>
            <tr><td>Exact match</td><td>{pct(fn.exact_match)}</td></tr>
            <tr><td>Exec rate</td><td>{pct(fn.exec_rate)}</td></tr>
            <tr><td>N échantillons</td><td>{fn.n_samples ?? '—'}</td></tr>
            <tr><td>Méthode</td><td><span class="method-chip">{fn.evaluator_method ?? '—'}</span></td></tr>
          </tbody>
        </table>
      </div>

      <!-- ── Performance ───────────────── -->
      <div class="dim-card">
        <div class="dim-head perf"><span>Performance opérationnelle</span></div>
        <table class="kpi-table">
          <tbody>
            <tr><td>Latence p50</td><td>{ms(fn.latency_p50)}</td></tr>
            <tr><td>Latence p95</td><td>{ms(fn.latency_p95)}</td></tr>
            <tr><td>Latence p99</td><td>{ms(fn.latency_p99)}</td></tr>
            <tr><td>Tokens/s</td><td>{raw(fn.tokens_per_second)}</td></tr>
            <tr><td>TTFT</td><td>{ms(fn.ttft_ms)}</td></tr>
            <tr><td>VRAM peak</td><td>{raw(fn.vram_peak_gb, 'Go')}</td></tr>
            <tr><td>Débit concurrent</td><td>{fn.throughput_concurrent != null ? fn.throughput_concurrent + ' req/s' : '—'}</td></tr>
            <tr><td>Adapter size</td><td>{raw(fn.adapter_size_mb, 'Mo')}</td></tr>
          </tbody>
        </table>
      </div>

      <!-- ── Évolution ─────────────────── -->
      <div class="dim-card">
        <div class="dim-head evolution"><span>Évolution / Régression</span></div>
        <table class="kpi-table">
          <tbody>
            <tr><td>Δ pass rate</td>
              <td class="{fn.delta_pass_rate != null ? (fn.delta_pass_rate >= 0 ? 'good' : 'bad') : ''}">
                {fn.delta_pass_rate != null ? (fn.delta_pass_rate >= 0 ? '+' : '') + pct(fn.delta_pass_rate) : '—'}
              </td>
            </tr>
            <tr><td>Regression rate</td><td>{pct(fn.regression_rate)}</td></tr>
            <tr><td>Version modèle</td><td>{fn.model_version ?? '—'}</td></tr>
            <tr><td>McNemar p-value</td><td>{score(fn.mcnemar_p_value)}</td></tr>
            <tr><td>McNemar winner</td><td>{fn.mcnemar_winner ?? '—'}</td></tr>
            <tr><td>Baseline run</td><td class="mono-cell">{fn.baseline_run_id ? fn.baseline_run_id.slice(-12) : '—'}</td></tr>
          </tbody>
        </table>
      </div>

      <!-- ── Robustesse ─────────────────── -->
      <div class="dim-card {rob ? '' : 'dim-pending'}">
        <div class="dim-head robustness"><span>Robustesse</span></div>
        {#if rob}
          <table class="kpi-table">
            <tbody>
              <tr><td>Consistance (×5)</td><td class="{deltaClass(rob.consistency_rate)}">{pct(rob.consistency_rate)}</td></tr>
              <tr><td>Pass rate T=0</td><td>{pct(rob.pass_rate_temp_0)}</td></tr>
              <tr><td>Pass rate T=0.3</td><td>{pct(rob.pass_rate_temp_03)}</td></tr>
              <tr><td>Pass rate T=0.7</td><td>{pct(rob.pass_rate_temp_07)}</td></tr>
              <tr><td>Dégradation T</td><td class="{rob.temperature_degradation != null ? (rob.temperature_degradation <= 0.05 ? 'good' : 'bad') : ''}">{pct(rob.temperature_degradation)}</td></tr>
              <tr><td>Forgetting cata.</td><td class="{rob.catastrophic_forgetting_rate != null ? (rob.catastrophic_forgetting_rate <= 0.05 ? 'good' : 'bad') : ''}">{pct(rob.catastrophic_forgetting_rate)}</td></tr>
            </tbody>
          </table>
        {:else}
          <div class="pending-msg">Non évalué — lancer via l'API</div>
        {/if}
      </div>

      <!-- ── Sécurité ───────────────────── -->
      <div class="dim-card {sec ? '' : 'dim-pending'}">
        <div class="dim-head security"><span>Sécurité</span></div>
        {#if sec}
          <table class="kpi-table">
            <tbody>
              <tr><td>Hallucination rate</td><td class="{deltaClass(sec.hallucination_rate, false)}">{pct(sec.hallucination_rate)}</td></tr>
              <tr><td>Refusal rate</td><td>{pct(sec.refusal_rate)}</td></tr>
              <tr><td>Data leakage</td><td class="{deltaClass(sec.sensitive_data_leakage_rate, false)}">{pct(sec.sensitive_data_leakage_rate)}</td></tr>
              <tr><td>Robustesse advers.</td><td class="{deltaClass(sec.adversarial_robustness)}">{pct(sec.adversarial_robustness)}</td></tr>
              <tr><td>Déterminisme</td><td class="{deltaClass(sec.output_determinism)}">{pct(sec.output_determinism)}</td></tr>
            </tbody>
          </table>
        {:else}
          <div class="pending-msg">Non évalué — lancer via l'API</div>
        {/if}
      </div>

      <!-- ── Conformité ─────────────────── -->
      <div class="dim-card {cmp ? '' : 'dim-pending'}">
        <div class="dim-head compliance"><span>Conformité secteur</span></div>
        {#if cmp}
          <table class="kpi-table">
            <tbody>
              <tr><td>Secteur</td><td><span class="sector-chip">{cmp.sector ?? '—'}</span></td></tr>
              <tr><td>Conformité format</td><td class="{deltaClass(cmp.format_compliance_rate)}">{pct(cmp.format_compliance_rate)}</td></tr>
              <tr><td>Citations réglementaires</td><td class="{deltaClass(cmp.regulatory_citation_accuracy)}">{pct(cmp.regulatory_citation_accuracy)}</td></tr>
              <tr><td>Détection PII</td><td class="{deltaClass(cmp.pii_detection_rate)}">{pct(cmp.pii_detection_rate)}</td></tr>
              <tr><td>Traceabilité audit</td><td class="{deltaClass(cmp.audit_traceability_score)}">{score(cmp.audit_traceability_score)}</td></tr>
            </tbody>
          </table>
        {:else}
          <div class="pending-msg">Non évalué — lancer via l'API</div>
        {/if}
      </div>

    </div>

    <!-- Category breakdown -->
    {#if cat.length > 0}
      <div class="section-card">
        <div class="section-title">Pass rate par catégorie</div>
        <CategoryBreakdown items={cat} />
      </div>
    {/if}

    <!-- MLflow link -->
    {#if fn.mlflow_run_id}
      <div class="mlflow-link">
        <a href="http://localhost:5001/#/experiments/0/runs/{fn.mlflow_run_id}" target="_blank" rel="noopener">
          → Voir dans MLflow
        </a>
      </div>
    {/if}

  {/if}
</div>

<style>
  .page { padding: var(--sp-6) var(--sp-8); max-width: 1100px; }

  .breadcrumb { display: flex; align-items: center; gap: 6px; margin-bottom: var(--sp-5); font-size: var(--text-sm); }
  .breadcrumb a { color: var(--accent); text-decoration: none; }
  .breadcrumb a:hover { text-decoration: underline; }
  .sep { color: var(--text-muted); }

  .run-header { display: flex; align-items: flex-start; justify-content: space-between; margin-bottom: var(--sp-6); flex-wrap: wrap; gap: var(--sp-4); }
  h1.run-id { font-size: var(--text-xl); font-weight: 700; color: var(--ink); font-family: var(--font-mono); }
  .proj-ref { font-size: 12px; color: var(--text-muted); }

  .dim-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: var(--sp-4); margin-bottom: var(--sp-5); }

  .dim-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); overflow: hidden; }
  .dim-pending { opacity: 0.65; }

  .dim-head {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 14px;
    font-size: var(--text-sm); font-weight: 600; color: var(--ink);
    background: var(--surface-3); border-bottom: 1px solid var(--border);
    border-left: 3px solid var(--border);
  }
  .dim-head.quality    { border-left-color: #10b981; }
  .dim-head.perf       { border-left-color: #f59e0b; }
  .dim-head.evolution  { border-left-color: var(--accent); }
  .dim-head.robustness { border-left-color: #8b5cf6; }
  .dim-head.security   { border-left-color: #ef4444; }
  .dim-head.compliance { border-left-color: #0ea5e9; }

  .kpi-table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
  .kpi-table td { padding: 7px 14px; border-bottom: 1px solid var(--border); }
  .kpi-table tr:last-child td { border-bottom: none; }
  .kpi-table td:first-child { color: var(--text-muted); font-size: var(--text-xs); font-weight: 500; width: 55%; }
  .kpi-table td:last-child  { font-weight: 600; font-variant-numeric: tabular-nums; }
  .kpi-table td.good  { color: #059669; }
  .kpi-table td.warn  { color: #d97706; }
  .kpi-table td.bad   { color: #dc2626; }
  .kpi-table td.mono-cell { font-family: var(--font-mono); font-size: 11px; }

  .method-chip  { font-size: 11px; background: #ede9fe; color: #5b21b6; padding: 2px 6px; border-radius: 4px; }
  .sector-chip  { font-size: 11px; background: #dbeafe; color: #1d4ed8; padding: 2px 6px; border-radius: 4px; }
  .warn-badge   { font-size: 10px; background: #fef3c7; color: #92400e; padding: 1px 6px; border-radius: 4px; }
  .badge        { display: inline-block; }

  .pending-msg { padding: 16px 14px; font-size: var(--text-xs); color: var(--text-muted); text-align: center; }

  .section-card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); padding: var(--sp-5); }
  .section-title { font-size: var(--text-sm); font-weight: 600; margin-bottom: var(--sp-4); }

  .mlflow-link { margin-top: var(--sp-4); font-size: var(--text-sm); }
  .mlflow-link a { color: var(--accent); text-decoration: none; font-weight: 500; }

  .loading, .error-msg { padding: var(--sp-8); text-align: center; font-size: var(--text-sm); color: var(--text-muted); }
  .error-msg { color: var(--red); }

  @media (max-width: 900px) { .dim-grid { grid-template-columns: 1fr; } }
</style>
