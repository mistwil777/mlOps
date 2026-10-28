<script lang="ts">
  import '../app.css';
  import { getHealth } from '$lib/api';
  import SidebarNav from '$lib/components/SidebarNav.svelte';
  import type { Health } from '$lib/types';

  let { children } = $props();
  let health: Health | null = $state(null);

  async function refreshHealth() {
    health = await getHealth().catch(() => null);
  }
  refreshHealth();
  setInterval(refreshHealth, 15000);
</script>

<div class="layout">
  <SidebarNav />

  <main class="main">
    <div class="rainbow-bar"></div>
    {@render children()}
  </main>
</div>

<style>
  .rainbow-bar {
    height: 4px;
    flex-shrink: 0;
    background: linear-gradient(90deg, #2DD4BF, #22D3EE, #38BDF8 40%, #A855F7 62%, #EC4899 80%, #F59E0B);
  }

  .health-panel {
    display: flex;
    flex-direction: column;
    gap: 5px;
  }

  .health-label {
    font-size: var(--text-xs);
    text-transform: uppercase;
    letter-spacing: .08em;
    color: var(--text-dim);
    font-weight: 600;
    margin-bottom: 2px;
  }

  .health-row {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }
</style>
