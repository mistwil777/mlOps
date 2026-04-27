<script lang="ts">
  import type { Snippet } from 'svelte';

  const {
    columns,
    rows,
    onRowClick,
    emptyMessage = 'Aucune donnée',
  }: {
    columns: { key: string; label: string; width?: string }[];
    rows: Record<string, unknown>[];
    onRowClick?: (row: Record<string, unknown>) => void;
    emptyMessage?: string;
  } = $props();
</script>

<div class="table-wrap">
  <table class="table">
    <thead>
      <tr>
        {#each columns as col}
          <th style={col.width ? `width:${col.width}` : ''}>{col.label}</th>
        {/each}
      </tr>
    </thead>
    <tbody>
      {#if rows.length === 0}
        <tr><td colspan={columns.length} style="text-align:center;color:var(--text-muted);padding:24px">{emptyMessage}</td></tr>
      {:else}
        {#each rows as row}
          <tr onclick={() => onRowClick?.(row)} style={onRowClick ? 'cursor:pointer' : ''}>
            {#each columns as col}
              <td>{row[col.key] ?? '—'}</td>
            {/each}
          </tr>
        {/each}
      {/if}
    </tbody>
  </table>
</div>

<style>
  .table-wrap { overflow-x: auto; }
</style>
