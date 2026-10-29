<script lang="ts">
  const {
    value,
    ci_low,
    ci_high,
    threshold = 0.7,
    label = '',
    height = 24,
    showCi = true,
  }: {
    value: number | null;
    ci_low?: number | null;
    ci_high?: number | null;
    threshold?: number;
    label?: string;
    height?: number;
    showCi?: boolean;
  } = $props();

  const W = 200;
  const BAR_H = 10;
  const y = (height - BAR_H) / 2;

  const pct = (v: number | null) => v != null ? Math.max(0, Math.min(1, v)) * W : 0;

  const color = $derived(
    value == null ? 'var(--text-muted)'
    : value >= threshold ? 'var(--green)'
    : value >= threshold * 0.8 ? 'var(--yellow)'
    : 'var(--red)'
  );
</script>

<div class="ci-bar-wrap">
  {#if label}
    <span class="ci-label">{label}</span>
  {/if}
  <div class="ci-bar-inner">
    <svg width="{W}" height="{height}" viewBox="0 0 {W} {height}">
      <!-- Background track -->
      <rect x="0" y="{y}" width="{W}" height="{BAR_H}" rx="5" fill="var(--surface-3)" />

      <!-- CI range -->
      {#if showCi && ci_low != null && ci_high != null}
        <rect
          x="{pct(ci_low)}"
          y="{y}"
          width="{pct(ci_high) - pct(ci_low)}"
          height="{BAR_H}"
          rx="2"
          fill="{color}"
          opacity="0.25"
        />
      {/if}

      <!-- Value fill -->
      {#if value != null}
        <rect
          x="0"
          y="{y}"
          width="{pct(value)}"
          height="{BAR_H}"
          rx="5"
          fill="{color}"
        />
      {/if}

      <!-- Threshold line -->
      <line
        x1="{pct(threshold)}"
        y1="{y - 3}"
        x2="{pct(threshold)}"
        y2="{y + BAR_H + 3}"
        stroke="var(--text-muted)"
        stroke-width="1.5"
        stroke-dasharray="3,2"
      />
    </svg>
    <span class="ci-value" style="color: {color}">
      {value != null ? (value * 100).toFixed(1) + '%' : '—'}
    </span>
    {#if showCi && ci_low != null && ci_high != null}
      <span class="ci-range">[{(ci_low * 100).toFixed(0)}–{(ci_high * 100).toFixed(0)}%]</span>
    {/if}
  </div>
</div>

<style>
  .ci-bar-wrap {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .ci-label {
    font-size: var(--text-xs);
    color: var(--text-muted);
    font-weight: 500;
  }
  .ci-bar-inner {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .ci-value {
    font-size: var(--text-sm);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    min-width: 46px;
  }
  .ci-range {
    font-size: 11px;
    color: var(--text-muted);
    font-variant-numeric: tabular-nums;
  }
</style>
