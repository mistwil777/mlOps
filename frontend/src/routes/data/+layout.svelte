<script lang="ts">
  let { children } = $props();
</script>

<div class="data-layout">
  {@render children()}
</div>

<style>
  .data-layout { display: flex; flex-direction: column; height: 100%; overflow: auto; }
</style>
