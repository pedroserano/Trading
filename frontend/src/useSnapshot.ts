import { useEffect, useState } from "react";
import type { Snapshot } from "./types";

export type Link = "connecting" | "open" | "retrying";

/** Live portfolio snapshot pushed by the backend over server-sent events. */
export function useSnapshot(account: string = "live"): {
  snapshot: Snapshot | null;
  link: Link;
} {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [link, setLink] = useState<Link>("connecting");

  useEffect(() => {
    setSnapshot(null);
    let source: EventSource | null = null;
    let retry: number | undefined;
    let closed = false;

    const connect = () => {
      source = new EventSource(`/api/events?account=${account}`);
      source.onopen = () => setLink("open");
      source.onmessage = (e) => {
        const snap = JSON.parse(e.data) as Snapshot;
        if (!snap.pending) setSnapshot(snap);
      };
      source.onerror = async () => {
        setLink("retrying");
        source?.close();
        if (closed || document.hidden) return;
        // A 401 means the app session ended; EventSource cannot see status codes.
        try {
          const r = await fetch("/api/snapshot", { cache: "no-store" });
          if (r.status === 401) {
            window.location.href = "/login";
            return;
          }
        } catch {
          /* network down: keep retrying */
        }
        if (!document.hidden) retry = window.setTimeout(connect, 3000);
      };
    };
    // In the background (a hidden tab, the phone app swapped out) the stream is closed, so
    // the server stops sending a snapshot a second; on return it reconnects and the first
    // message is the current snapshot.
    const onVisibility = () => {
      if (document.hidden) {
        window.clearTimeout(retry);
        source?.close();
        source = null;
      } else if (!source || source.readyState === EventSource.CLOSED) {
        window.clearTimeout(retry);
        connect();
      }
    };
    if (!document.hidden) connect();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      closed = true;
      source?.close();
      window.clearTimeout(retry);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [account]);

  return { snapshot, link };
}
