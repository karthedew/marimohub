<script lang="ts">
	import SessionFrame from '$lib/components/SessionFrame.svelte';

	let { params } = $props();
</script>

<SessionFrame notebookId={params.id} mode="edit" heading="Edit notebook" backHref={`/notebooks/${params.id}`} backLabel="Back to notebook" />
