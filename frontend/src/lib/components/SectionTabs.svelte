<script lang="ts">
  import { page } from '$app/stores';

  interface Tab { href: string; label: string; }
  interface Props { tabs: Tab[]; }
  let { tabs }: Props = $props();

  const projectId = $derived($page.url.searchParams.get('project'));

  function href(base: string) {
    return projectId ? `${base}?project=${projectId}` : base;
  }

  function isActive(base: string) {
    const p = $page.url.pathname;
    return p === base || p.startsWith(base + '/');
  }
</script>

<div class="stabs">
  {#each tabs as t}
    <a href={href(t.href)} class="stab" class:active={isActive(t.href)}>{t.label}</a>
  {/each}
</div>

<style>
  .stabs {
    display: flex; gap: 2px;
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    background: var(--bg);
    flex-shrink: 0;
  }
  .stab {
    padding: 10px 16px; font-size: 13px;
    color: var(--text-muted); text-decoration: none;
    border-bottom: 2px solid transparent; margin-bottom: -1px;
  }
  .stab:hover { color: var(--text); }
  .stab.active { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
</style>
