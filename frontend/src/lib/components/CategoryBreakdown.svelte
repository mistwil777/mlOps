<script lang="ts">
  export interface CategoryItem {
    category: string;
    pass_rate: number;
    ci_low: number;
    ci_high: number;
    n: number;
  }

  const { items = [], threshold = 0.7 }: { items?: CategoryItem[]; threshold?: number } = $props();

  const W = 280;
  const ROW_H = 28;
  const BAR_MAX_W = 160;
  const LABEL_W = 90;
  const VALUE_W = 48;

  const color = (v: number) =>
    v >= threshold ? 'var(--green)' : v >= threshold * 0.8 ? 'var(--yellow)' : 'var(--red)';

  const totalH = $derived(items.length * ROW_H + 8);
</script>

<div class="cat-wrap">
  <svg
    width="{LABEL_W + BAR_MAX_W + VALUE_W + 16}"
    height="{totalH}"
    viewBox="0 0 {LABEL_W + BAR_MAX_W + VALUE_W + 16} {totalH}"
  >
    {#each items as item, i}
      {@const y = i * ROW_H + 4}
      {@const barW = item.pass_rate * BAR_MAX_W}
      {@const ciX1 = item.ci_low * BAR_MAX_W}
      {@const ciX2 = item.ci_high * BAR_MAX_W}
      {@const c = color(item.pass_rate)}

      <!-- Label -->
      <text x="{LABEL_W - 8}" y="{y + 17}" text-anchor="end" font-size="12" fill="var(--text-muted)" font-family="Inter">{item.category}</text>

      <!-- Background track -->
      <rect x="{LABEL_W}" y="{y + 9}" width="{BAR_MAX_W}" height="10" rx="5" fill="var(--surface-3)" />

      <!-- CI band -->
      <rect x="{LABEL_W + ciX1}" y="{y + 9}" width="{ciX2 - ciX1}" height="10" rx="3" fill="{c}" opacity="0.2" />

      <!-- Bar -->
      <rect x="{LABEL_W}" y="{y + 9}" width="{barW}" height="10" rx="5" fill="{c}" />

      <!-- Value -->
      <text x="{LABEL_W + BAR_MAX_W + 8}" y="{y + 18}" font-size="12" font-weight="600" fill="{c}" font-family="Inter">
        {(item.pass_rate * 100).toFixed(0)}%
      </text>

      <!-- n -->
      <text x="{LABEL_W + BAR_MAX_W + VALUE_W + 8}" y="{y + 18}" font-size="10" fill="var(--text-muted)" font-family="Inter">{`n=${item.n}`}</text>
    {/each}
  </svg>
</div>

<style>
  .cat-wrap {
    overflow-x: auto;
  }
</style>
