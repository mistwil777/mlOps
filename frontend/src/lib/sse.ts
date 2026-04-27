/**
 * Wrapper autour de EventSource avec reconnect automatique.
 * Retourne un Svelte store-compatible (subscribe).
 */

type Handler<T> = (data: T) => void;
type CleanupFn  = () => void;

export function sseStream<T = unknown>(
  url: string,
  onEvent: Handler<T>,
  eventName = 'message',
  onError?: (e: Event) => void,
): CleanupFn {
  let es: EventSource | null = null;
  let closed = false;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  function connect() {
    if (closed) return;
    es = new EventSource(url);

    es.addEventListener(eventName, (e: MessageEvent) => {
      try {
        onEvent(JSON.parse(e.data) as T);
      } catch {
        onEvent(e.data as unknown as T);
      }
    });

    es.onerror = (e) => {
      onError?.(e);
      es?.close();
      if (!closed) {
        retryTimer = setTimeout(connect, 3000);
      }
    };
  }

  connect();

  return () => {
    closed = true;
    if (retryTimer) clearTimeout(retryTimer);
    es?.close();
  };
}

/** Subscribe to ALL named events on one SSE connection. */
export function sseMulti(
  url: string,
  handlers: Record<string, Handler<unknown>>,
): CleanupFn {
  let es: EventSource | null = null;
  let closed = false;

  function connect() {
    if (closed) return;
    es = new EventSource(url);

    for (const [eventName, handler] of Object.entries(handlers)) {
      es.addEventListener(eventName, (e: MessageEvent) => {
        try { handler(JSON.parse(e.data)); }
        catch { handler(e.data); }
      });
    }

    es.onerror = () => {
      es?.close();
      if (!closed) setTimeout(connect, 3000);
    };
  }

  connect();
  return () => { closed = true; es?.close(); };
}
