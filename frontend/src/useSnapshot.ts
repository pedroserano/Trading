import { useEffect, useState } from "react";
import type { Snapshot } from "./types";

export type Link = "connecting" | "open" | "retrying";

/** Live portfolio snapshot pushed by the backend over server-sent events. */
export function useSnapshot(account: "live" | "sim" = "live"): {
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
        if (closed) return;
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
        retry = window.setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      closed = true;
      source?.close();
      window.clearTimeout(retry);
    };
  }, [account]);

  return { snapshot, link };
}
