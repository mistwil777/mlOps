<script lang="ts">
  export interface KpiColumnDef {
    key: string;
    label: string;
    format: 'pct' | 'ms' | 'Mo' | 'tps' | 'score' | 'raw';
    higher_is_better: boolean;
  }

  export interface KpiRunColumn {
    run_id: string;
    version_tag: string;
    kpis: Record<string, number | null>;
  }

  const {
    runs = [],
    metrics = [],
  }: { runs?: KpiRunColumn[]; metrics?: KpiColumnDef[] } = $props();

  function fmt(v: number | null, format: string): string {
    if (v == null) return '—';
    switch (format) {
      case 'pct':   return (v * 100).toFixed(1) + '%';
      case 'ms':    return v.toFixed(0) + ' ms';
      case 'Mo':    return v.toFixed(1) + ' Mo';
      case 'tps':   return v.toFixed(1);
      case 'score': return v.toFixed(3);
      default:      return String(v);
    }
  }

  function fmtDelta(delta: number, format: string): string {
    const sign = delta >= 0 ? '+' : '';
    switch (format) {
      case 'ms':  return sign + delta.toFixed(0) + 'ms';
      case 'Mo':  return sign + delta.toFixed(1) + ' Mo';
      case 'tps': return sign + delta.toFixed(1) + ' t/s';
      case 'pct': return sign + (delta * 100).toFixed(1) + 'pp';
      default:    return sign + (delta * 100).toFixed(1) + 'pp';
    }
  }

  function deltaClass(base: number | null, cmp: number | null, higherBetter: boolean): string {
    if (base == null || cmp == null) return '';
    const better = higherBetter ? cmp > base : cmp < base;
    const same = Math.abs(cmp - base) < 0.001;
    if (same) return '';
    return better ? 'better' : 'worse';
  }

  const baseRun = $derived(runs[0]);
</script>

{#if runs.length === 0}
  <p class="empty">Aucun run sélectionné.</p>
{:else}
  <div class="compare-wrap">
    <table class="compare-table">
      <thead>
        <tr>
          <th class="metric-col">Métrique</th>
          {#each runs as run}
            <th class="run-col">
              <span class="version-tag">{run.version_tag}</span>
              <span class="run-id">{run.run_id.slice(-8)}</span>
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each metrics as m}
          <tr>
            <td class="metric-name">{m.label}</td>
            {#each runs as run, i}
              {@const val = run.kpis[m.key] ?? null}
              {@const base = baseRun?.kpis[m.key] ?? null}
              {@const cls = i === 0 ? '' : deltaClass(base, val, m.higher_is_better)}
              <td class="kpi-cell {cls}">
                {fmt(val, m.format)}
                {#if i > 0 && base != null && val != null}
                  {@const delta = (m.format === 'ms' || m.format === 'Mo' || m.format === 'tps') ? val - base : val - base}
                  <span class="delta {cls}">
                    {fmtDelta(delta, m.format)}
                  </span>
                {/if}
              </td>
            {/each}
          </tr>
        {/each}
      </tbody>
    </table>
  </div>
{/if}

<style>
  .compare-wrap { overflow-x: auto; }
  .compare-table {
    width: 100%;
    border-collapse: collapse;
    font-size: var(--text-sm);
  }
  th {
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
  td {
    padding: 10px 14px;
    border-bottom: 1px solid var(--border);
    color: var(--ink-2);
    font-variant-numeric: tabular-nums;
    vertical-align: middle;
  }
  .metric-col { min-width: 180px; }
  .run-col { min-width: 130px; }
  .metric-name { font-weight: 500; color: var(--ink); }
  .version-tag { font-weight: 700; color: var(--accent); font-size: var(--text-sm); display: block; }
  .run-id { font-size: 10px; color: var(--text-muted); font-family: var(--font-mono); }
  .kpi-cell { font-weight: 500; }
  .kpi-cell.better { color: var(--green); }
  .kpi-cell.worse  { color: var(--red); }
  .delta {
    display: inline-block;
    font-size: 11px;
    font-weight: 600;
    margin-left: 6px;
    padding: 1px 5px;
    border-radius: 4px;
  }
  .delta.better { background: #d1fae5; color: #065f46; }
  .delta.worse  { background: #fee2e2; color: #991b1b; }
  .empty { color: var(--text-muted); font-size: var(--text-sm); padding: 12px 0; }
</style>
