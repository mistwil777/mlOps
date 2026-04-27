<script lang="ts">
  const { domain, taskType }: { domain: string; taskType: string } = $props();
</script>

<span class="task-badge">
  <span class="domain">{domain}</span>
  <span class="sep">/</span>
  <span class="task">{taskType}</span>
</span>

<style>
  .task-badge { display: inline-flex; align-items: center; gap: 4px; font-family: var(--font-mono); font-size: 12px; }
  .domain { background: var(--surface-2); padding: 2px 8px; border-radius: 4px; color: var(--accent); }
  .sep { color: var(--text-muted); }
  .task { background: var(--surface-2); padding: 2px 8px; border-radius: 4px; color: var(--text-muted); }
</style>
