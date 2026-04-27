<script lang="ts">
  const {
    vramBase,
    batchSize,
    loraRank,
  }: { vramBase: number; batchSize: number; loraRank: number } = $props();

  // Approximate VRAM = model base + overhead per batch + lora overhead
  const estimated = $derived(
    Math.round((vramBase + batchSize * 0.15 + loraRank * 0.05) * 10) / 10
  );
  const pct = $derived(Math.min(100, Math.round((estimated / 48) * 100)));
  const color = $derived(pct > 85 ? 'var(--red)' : pct > 65 ? 'var(--yellow)' : 'var(--green)');
</script>

<div class="estimator">
  <div class="flex items-center justify-between mb-8">
    <span class="text-muted" style="font-size:12px">VRAM estimé</span>
    <span style="font-weight:700;color:{color}">{estimated} GB / 48 GB</span>
  </div>
  <div class="bar-bg">
    <div class="bar-fill" style="width:{pct}%;background:{color}"></div>
  </div>
</div>

<style>
  .estimator { padding: 12px; background: var(--surface-2); border-radius: var(--radius); border: 1px solid var(--border); }
  .bar-bg { height: 6px; background: var(--surface-3); border-radius: 3px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 3px; transition: width 300ms, background 300ms; }
</style>
