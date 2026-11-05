<script lang="ts">
  import '../app.css';
  import { getHealth } from '$lib/api';
  import SidebarNav from '$lib/components/SidebarNav.svelte';
  import PipelineProgress from '$lib/components/PipelineProgress.svelte';
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

  <div class="content-col">
    <header class="app-header">
      <div class="rainbow-bar"></div>
      <PipelineProgress />
    </header>

    <main class="main">
      {@render children()}
    </main>
  </div>
</div>

<style>
  .content-col {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .app-header {
    flex-shrink: 0;
  }

  .rainbow-bar {
    height: 4px;
    background: linear-gradient(90deg, #2DD4BF, #22D3EE, #38BDF8 40%, #A855F7 62%, #EC4899 80%, #F59E0B);
  }
</style>
