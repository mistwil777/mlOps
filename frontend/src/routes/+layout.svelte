<script lang="ts">
  import '../app.css';
  import { page } from '$app/stores';
  import { getHealth } from '$lib/api';
  import type { Health } from '$lib/types';

  let { children } = $props();
  let health: Health | null = $state(null);

  async function refreshHealth() {
    health = await getHealth().catch(() => null);
  }
  refreshHealth();
  setInterval(refreshHealth, 15000);

  const nav = [
    { href: '/',             icon: '⬡', label: 'Dashboard' },
    { href: '/projects',     icon: '◈', label: 'Projets',      section: 'Factory' },
    { href: '/base-models',  icon: '◫', label: 'Modèles base' },
    { href: '/data',         icon: '◧', label: 'Données',      section: 'Data' },
    { href: '/rag',          icon: '◩', label: 'RAG' },
    { href: '/train',        icon: '▷', label: 'Fine-tuning',  section: 'Training' },
    { href: '/models',       icon: '◉', label: 'Modèles fine-tunés' },
    { href: '/adapters',     icon: '◈', label: 'Adaptateurs LoRA' },
    { href: '/playground',   icon: '◐', label: 'Playground',   section: 'Test' },
    { href: '/delivery',     icon: '⬡', label: 'Livraison',    section: 'Delivery' },
    { href: '/lifecycle',    icon: '✈', label: 'Sol ↔ Embarqué', section: 'Embarqué' },
  ];

  function isActive(href: string) {
    if (href === '/') return $page.url.pathname === '/';
    return $page.url.pathname.startsWith(href);
  }
</script>

<div class="layout">
  <aside class="sidebar">
    <div class="sidebar-logo">
      <div class="dot"></div>
      MLOps Factory
    </div>

    <nav>
      {#each nav as item}
        {#if item.section}
          <div class="nav-section">{item.section}</div>
        {/if}
        <a href={item.href} class:active={isActive(item.href)}>
          <span style="font-size:16px">{item.icon}</span>
          {item.label}
        </a>
      {/each}
    </nav>

    <div class="sidebar-health">
      <div class="health-row">
        <span class="health-dot {health?.mlx_server ? 'ok' : health === null ? 'unk' : 'err'}"></span>
        <span>mlx_lm.server</span>
      </div>
      <div class="health-row">
        <span class="health-dot {health?.mlflow ? 'ok' : health === null ? 'unk' : 'err'}"></span>
        <span>MLflow</span>
      </div>
      <div class="health-row">
        <span class="health-dot {health?.serving ? 'ok' : health === null ? 'unk' : 'err'}"></span>
        <span>Serving</span>
      </div>
    </div>
  </aside>

  <main class="main">
    {@render children()}
  </main>
</div>

<style>
  .sidebar-health {
    padding: 12px 16px;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .health-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
    color: var(--text-muted);
  }
</style>
