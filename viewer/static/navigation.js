// Route loading with generation checks for the read-only memory viewer.

export function createRouteNavigation(dataSource, onResult) {
  let generation = 0;
  const isCurrent = (candidate) => candidate === generation;

  async function render(languageId, requestedPath, languages) {
    const currentGeneration = ++generation;
    if (!languageId) {
      onResult({ kind: "picker", generation: currentGeneration });
      return;
    }

    const language = languages.find((item) => item.id === languageId);
    if (!language) {
      onResult({ kind: "picker", generation: currentGeneration });
      return;
    }

    onResult({
      kind: "loading",
      generation: currentGeneration,
      language,
      requestedPath,
    });

    try {
      const groups = await dataSource.listDocuments(language.id);
      if (!isCurrent(currentGeneration)) return;
      const documents = groups.flatMap((group) => group.documents);
      const selected = requestedPath
        ? documents.find(({ path }) => path === requestedPath)
        : null;
      const target = requestedPath
        ? selected
        : documents[0] || null;

      if (!target) {
        onResult({
          kind: requestedPath ? "missing" : "empty",
          generation: currentGeneration,
          language,
          requestedPath,
          groups,
        });
        return;
      }

      const targetGroup = target.group || groups.find((group) =>
        group.documents.some((item) => item.path === target.path)
      )?.id;
      const documentData = await dataSource.getDocument(language.id, target.path);
      if (!isCurrent(currentGeneration)) return;

      onResult({
        kind: "document",
        generation: currentGeneration,
        language,
        requestedPath,
        groups,
        target,
        targetGroup,
        documentData,
      });
    } catch (error) {
      if (!isCurrent(currentGeneration)) return;
      onResult({
        kind: "error",
        generation: currentGeneration,
        language,
        requestedPath,
        error,
      });
    }
  }

  return { render, isCurrent };
}
