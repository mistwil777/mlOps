<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';

  const project = $derived($page.url.searchParams.get('project'));

  $effect(() => {
    goto(project ? `/data/donnees?project=${project}` : '/data/donnees', { replaceState: true });
  });
</script>
