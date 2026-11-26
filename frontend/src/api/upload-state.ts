/** Server document snapshots can lag behind a newly started retry execution. */
export function reconcileDocumentSnapshot<
  T extends {
    documentId: string;
    status: string;
    retryExecutionId?: string;
  },
>(previous: T[], incoming: T[]): T[] {
  const byId = new Map(previous.map((item) => [item.documentId, item]));
  const ids = new Set(incoming.map((item) => item.documentId));
  return [
    ...previous.filter((item) => !ids.has(item.documentId)),
    ...incoming.map((item) => {
      const pending = byId.get(item.documentId);
      return pending?.retryExecutionId && item.status !== 'ready' ? pending : item;
    }),
  ];
}
