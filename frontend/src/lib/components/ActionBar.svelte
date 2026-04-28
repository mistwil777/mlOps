<script lang="ts">
  import type { NextAction } from '$lib/types';

  interface Props {
    actions: NextAction[];
  }

  let { actions }: Props = $props();
</script>

{#if actions.length > 0}
  <div class="action-bar">
    <span class="action-bar-label">Actions recommandées</span>
    <div class="action-list">
      {#each actions as action}
        <a href={action.href} class="action-item priority-{action.priority}" title={action.detail}>
          <span class="action-icon">{action.icon}</span>
          <span class="action-label">{action.label}</span>
          <span class="action-arrow">→</span>
        </a>
      {/each}
    </div>
  </div>
{/if}

<style>
  .action-bar {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 10px 14px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
  }

  .action-bar-label {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--text-dim);
    font-weight: 700;
    flex-shrink: 0;
    padding-top: 3px;
    white-space: nowrap;
  }

  .action-list {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    flex: 1;
  }

  .action-item {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    border-radius: var(--radius);
    font-size: var(--text-sm);
    text-decoration: none;
    border: 1px solid transparent;
    transition: background 150ms, border-color 150ms, color 150ms;
  }

  .action-item.priority-high {
    background: var(--red-dim);
    border-color: rgba(247,84,84,.25);
    color: var(--red);
  }

  .action-item.priority-high:hover {
    background: rgba(247,84,84,.2);
    border-color: var(--red);
    text-decoration: none;
  }

  .action-item.priority-medium {
    background: var(--yellow-dim);
    border-color: rgba(247,170,80,.25);
    color: var(--yellow);
  }

  .action-item.priority-medium:hover {
    background: rgba(247,170,80,.2);
    border-color: var(--yellow);
    text-decoration: none;
  }

  .action-item.priority-low {
    background: var(--surface-3);
    border-color: var(--border-2);
    color: var(--text-muted);
  }

  .action-item.priority-low:hover {
    background: var(--surface-3);
    border-color: var(--border-2);
    color: var(--text);
    text-decoration: none;
  }

  .action-icon  { font-size: 12px; flex-shrink: 0; }
  .action-arrow { font-size: 11px; opacity: .7; margin-left: 2px; }
</style>
