<script lang="ts">
  const { src, tgt }: { src: string; tgt: string } = $props();
</script>

<span class="pair">
  <span class="lang">{src}</span>
  <span class="arrow">→</span>
  <span class="lang">{tgt}</span>
</span>

<style>
  .pair { display: inline-flex; align-items: center; gap: 6px; font-family: var(--font-mono); font-size: 13px; }
  .lang { background: var(--surface-2); padding: 2px 8px; border-radius: 4px; color: var(--accent); }
  .arrow { color: var(--text-muted); }
</style>
