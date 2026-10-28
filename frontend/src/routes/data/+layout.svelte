<script lang="ts">
  import { page } from '$app/stores';
  let { children } = $props();

  const tabs = [
    { href: '/advisor',       label: 'Cadrage' },
    { href: '/data/ingest',   label: 'Ingestion' },
    { href: '/data/pipeline', label: 'Pipeline' },
    { href: '/data/review',   label: 'Révision' },
    { href: '/data/search',   label: 'Audit' },
  ];

  const projectId = $derived($page.url.searchParams.get('project'));

  function tabHref(base: string) {
    return projectId ? `${base}?project=${projectId}` : base;
  }

  function isActive(href: string) {
    return $page.url.pathname === href || $page.url.pathname.startsWith(href + '/');
  }
</script>

<div class="data-layout">
  <div class="data-tabs">
    {#each tabs as t}
      <a href={tabHref(t.href)} class="data-tab" class:active={isActive(t.href)}>{t.label}</a>
    {/each}
  </div>
  <div class="data-content">
    {@render children()}
  </div>
</div>

<style>
  .data-layout { display: flex; flex-direction: column; height: 100%; }
  .data-tabs {
    display: flex;
    gap: 2px;
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    background: var(--surface-1);
  }
  .data-tab {
    padding: 10px 16px;
    font-size: 13px;
    color: var(--text-muted);
    text-decoration: none;
    border-bottom: 2px solid transparent;
    margin-bottom: -1px;
  }
  .data-tab:hover { color: var(--text); }
  .data-tab.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
  .data-content { flex: 1; overflow: auto; }
</style>
