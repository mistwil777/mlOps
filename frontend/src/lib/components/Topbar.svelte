<script lang="ts">
  interface BreadcrumbItem {
    label: string;
    href?: string;
  }

  interface Props {
    crumbs?: BreadcrumbItem[];
    title?: string;
  }

  let { crumbs = [], title }: Props = $props();
</script>

<header class="topbar">
  {#if crumbs.length > 0}
    <nav class="topbar-breadcrumb" aria-label="Breadcrumb">
      {#each crumbs as crumb, i}
        {#if i > 0}<span class="bc-sep">/</span>{/if}
        {#if crumb.href && i < crumbs.length - 1}
          <a href={crumb.href}>{crumb.label}</a>
        {:else}
          <span class="bc-current">{crumb.label}</span>
        {/if}
      {/each}
    </nav>
  {:else if title}
    <h1 class="topbar-title">{title}</h1>
  {/if}

  <slot />
</header>
