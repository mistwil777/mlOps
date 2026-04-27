<script lang="ts">
  const {
    label,
    value,
    sub = '',
    color = '',
  }: { label: string; value: string | number; sub?: string; color?: string } = $props();
</script>

<div class="metric">
  <div class="metric-label">{label}</div>
  <div class="metric-value" style={color ? `color:${color}` : ''}>{value ?? '—'}</div>
  {#if sub}<div class="metric-sub">{sub}</div>{/if}
</div>
