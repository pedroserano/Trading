import { useEffect, useState } from "react";

/**
 * The Settings page. Its content is rendered by the server (forms for the Schwab sign-in,
 * uploads and rules post straight to it) and shown here, under the same header and menu as
 * every other page.
 */
export default function Settings({ section }: { section: string | null }) {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    fetch("/connect?fragment=1", { cache: "no-store" })
      .then(async (r) => {
        if (r.url.endsWith("/login")) {
          window.location.href = "/login";
          return;
        }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const text = await r.text();
        if (!stop) setHtml(text);
      })
      .catch((e) => !stop && setError(String((e as Error).message)));
    return () => {
      stop = true;
    };
  }, []);

  useEffect(() => {
    if (html && section) document.getElementById(section)?.scrollIntoView();
  }, [html, section]);

  if (error) return <div className="banner warn">Settings could not be loaded: {error}</div>;
  if (html === null) return <div className="empty">Loading…</div>;
  // Server-rendered by this app with every value escaped.
  return <div className="settings-page" dangerouslySetInnerHTML={{ __html: html }} />;
}
