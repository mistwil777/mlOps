<script lang="ts">
  const { status }: { status: string } = $props();
  const dot = ['running', 'serving'].includes(status);
</script>

<span class="badge badge-{status}">
  {#if dot}<span class="pulse"></span>{/if}
  {status}
</span>

<style>
  .pulse {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: currentColor;
    display: inline-block;
    animation: pulse 1.4s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: .3; }
  }
</style>
