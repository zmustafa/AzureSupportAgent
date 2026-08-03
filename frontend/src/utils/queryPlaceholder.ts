/**
 * Keep the previous page of data on screen — but never across a tenant change.
 *
 * `placeholderData: keepPreviousData` exists so a grid does not flash empty on every keystroke
 * or filter toggle. That is the right behaviour while the *question* changes and the *subject*
 * does not.
 *
 * It is the wrong behaviour when the connection changes. The tenant picker updates immediately,
 * so for the whole length of the refetch the screen shows one organisation's rows underneath
 * another organisation's name, with no spinner and nothing to indicate the mismatch. A reader
 * who glances at a count during that window attributes it to the wrong tenant — and on a large
 * estate the window is long enough to read, act on, and screenshot.
 *
 * Dropping the placeholder makes the grid go briefly empty on a tenant switch, which is honest:
 * we genuinely do not know the answer for the new tenant yet.
 *
 * @param connectionId  the connection now selected
 * @param fromKey       pulls the connection out of a query key; call sites differ, so this is
 *                      explicit rather than assuming a position
 */
export function keepPreviousWithinConnection<T>(
  connectionId: string | null | undefined,
  fromKey: (queryKey: readonly unknown[]) => unknown,
): (prev: T | undefined, prevQuery: { queryKey: readonly unknown[] } | undefined) => T | undefined {
  const current = connectionId ?? "";
  return (prev, prevQuery) => {
    if (!prev || !prevQuery) return prev;
    const previous = fromKey(prevQuery.queryKey) ?? "";
    return previous === current ? prev : undefined;
  };
}
