<script lang="ts">
  const { value, max, label, size = 80 }: { value: number; max: number; label: string; size?: number } = $props();
  const r = 28;
  const circ = $derived(2 * Math.PI * r);
  const pct  = $derived(Math.min(1, value / max));
  const dash  = $derived(circ * pct);
  const color = $derived(pct > .85 ? 'var(--red)' : pct > .65 ? 'var(--yellow)' : 'var(--accent)');
</script>

<div class="ring-wrap" style="width:{size}px;height:{size}px;">
  <svg viewBox="0 0 64 64" width={size} height={size}>
    <circle cx="32" cy="32" r={r} fill="none" stroke="var(--surface-3)" stroke-width="5"/>
    <circle cx="32" cy="32" r={r} fill="none"
      stroke={color} stroke-width="5"
      stroke-dasharray="{dash} {circ}"
      stroke-linecap="round"
      transform="rotate(-90 32 32)"
      style="transition: stroke-dasharray 400ms ease;"
    />
  </svg>
  <div class="ring-label">
    <span class="ring-val">{Math.round(pct * 100)}%</span>
    <span class="ring-sub">{label}</span>
  </div>
</div>

<style>
  .ring-wrap { position: relative; display: inline-flex; align-items: center; justify-content: center; }
  .ring-wrap svg { position: absolute; top: 0; left: 0; }
  .ring-label { display: flex; flex-direction: column; align-items: center; }
  .ring-val { font-size: 14px; font-weight: 700; line-height: 1; }
  .ring-sub { font-size: 10px; color: var(--text-muted); text-align: center; margin-top: 2px; }
</style>
