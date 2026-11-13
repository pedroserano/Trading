import { useCallback, useEffect, useRef, useState } from "react";

/**
 * The Settings page. Its content is rendered by the server (forms for the Schwab sign-in,
 * rules and the rest post straight to it) and shown here in tabs, under the same header and
 * menu as every other page. Each tab's .tab-panel comes from the server; this component
 * adds the tab bar, marks values changed from their defaults, resets a group, and warns
 * before unsaved changes are lost.
 */

type Tab = { key: string; label: string };

function inputs(root: ParentNode): (HTMLInputElement | HTMLSelectElement)[] {
  return Array.from(root.querySelectorAll<HTMLInputElement | HTMLSelectElement>("[data-default]"));
}

function valueOf(el: HTMLInputElement | HTMLSelectElement): string {
  if (el instanceof HTMLInputElement && el.type === "checkbox") return el.checked ? "1" : "0";
  return el.value;
}

function isDefault(el: HTMLInputElement | HTMLSelectElement): boolean {
  const d = el.dataset.default ?? "";
  if (el instanceof HTMLInputElement && el.type === "number") return Number(el.value) === Number(d);
  return valueOf(el) === d;
}

/** Shows the ● on each row whose value differs from its default. */
function markChanged(root: ParentNode) {
  for (const el of inputs(root)) {
    // A setting written into the explanation marks itself; a row marks its ● instead.
    if (el.classList.contains("inl")) el.classList.toggle("is-changed", !isDefault(el));
    else el.closest(".rule")?.classList.toggle("is-changed", !isDefault(el));
  }
}

export default function Settings({ section }: { section: string | null }) {
  const [html, setHtml] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [dirty, setDirty] = useState<Set<string>>(new Set());
  const page = useRef<HTMLDivElement>(null);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;
  const submitting = useRef(false);

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

  // Tabs from the server's panels; the section in the address picks one (a tab's own name,
  // or the id of something inside it, as the server's redirects after a save use).
  useEffect(() => {
    const el = page.current;
    if (!html || !el) return;
    const panels = Array.from(el.querySelectorAll<HTMLElement>(".tab-panel"));
    setTabs(panels.map((p) => ({ key: p.dataset.tab ?? "", label: p.dataset.label ?? "" })));
    let pick = panels[0]?.dataset.tab ?? null;
    if (section) {
      const own = panels.find((p) => p.dataset.tab === section);
      const holder = own ?? document.getElementById(section)?.closest<HTMLElement>(".tab-panel");
      if (holder?.dataset.tab) pick = holder.dataset.tab;
    }
    setActive(pick);
    markChanged(el);
  }, [html, section]);

  useEffect(() => {
    const el = page.current;
    if (!el || !active) return;
    for (const p of el.querySelectorAll<HTMLElement>(".tab-panel")) p.hidden = p.dataset.tab !== active;
    el.classList.add("tabbed");
  }, [active, html]);

  const choose = useCallback((key: string) => {
    setActive(key);
    // Keep the address in step without a navigation, so a reload stays on this tab.
    window.history.replaceState(null, "", `#/settings/${key}`);
    window.scrollTo({ top: 0 });
  }, []);

  // Edits: mark changed values and the tab with unsaved changes; the margin type and the
  // delta bias reset other settings, so choosing one saves at once.
  useEffect(() => {
    const el = page.current;
    if (!el) return;
    const panelOf = (t: EventTarget | null) => (t as HTMLElement | null)?.closest<HTMLElement>(".tab-panel");
    const tabOf = (t: EventTarget | null) => panelOf(t)?.dataset.tab;
    const onEdit = (e: Event) => {
      const t = e.target as HTMLInputElement | HTMLSelectElement;
      if (!t.form) return;
      if (e.type === "change" && (t.id === "r-margin" || t.id === "r-bias")) {
        t.form.requestSubmit();
        return;
      }
      markChanged(t.closest("form") ?? el);
      if (t.form.method.toLowerCase() === "post" && t.form.querySelector("[data-default]")) {
        t.form.dataset.dirty = "1";
        const tab = tabOf(t);
        if (tab) setDirty((s) => (s.has(tab) ? s : new Set(s).add(tab)));
      }
    };
    const onClick = (e: Event) => {
      const b = (e.target as HTMLElement).closest<HTMLButtonElement>("button.reset-group");
      if (!b) return;
      e.preventDefault();
      const group = b.closest(".group");
      if (!group) return;
      for (const i of inputs(group)) {
        if (i instanceof HTMLInputElement && i.type === "checkbox") i.checked = i.dataset.default === "1";
        else i.value = i.dataset.default ?? i.value;
      }
      const form = b.closest("form");
      if (form) {
        form.dataset.dirty = "1";
        const tab = tabOf(b);
        if (tab) setDirty((s) => new Set(s).add(tab));
      }
      markChanged(group);
    };
    // Saving one form reloads the page: warn when another has unsaved changes.
    const onSubmit = (e: Event) => {
      const form = e.target as HTMLFormElement;
      const others = Array.from(el.querySelectorAll<HTMLFormElement>("form[data-dirty]")).filter((f) => f !== form);
      if (others.length) {
        const where = [...new Set(others.map((f) => panelOf(f)?.dataset.label).filter(Boolean))].join(", ");
        if (!window.confirm(`Unsaved changes on ${where} will be lost. Save this anyway?`)) {
          e.preventDefault();
          return;
        }
      }
      submitting.current = true;
    };
    el.addEventListener("input", onEdit);
    el.addEventListener("change", onEdit);
    el.addEventListener("click", onClick);
    el.addEventListener("submit", onSubmit, true);
    return () => {
      el.removeEventListener("input", onEdit);
      el.removeEventListener("change", onEdit);
      el.removeEventListener("click", onClick);
      el.removeEventListener("submit", onSubmit, true);
    };
  }, [html]);

  // Leaving Settings with unsaved changes: the menu's links and a reload ask first.
  useEffect(() => {
    const onLink = (e: MouseEvent) => {
      if (!dirtyRef.current.size) return;
      const a = (e.target as HTMLElement).closest<HTMLAnchorElement>("a[href^='#/']");
      if (!a || a.getAttribute("href")?.startsWith("#/settings")) return;
      if (!window.confirm("You have unsaved changes in Settings. Leave without saving?")) e.preventDefault();
    };
    const onUnload = (e: BeforeUnloadEvent) => {
      if (dirtyRef.current.size && !submitting.current) e.preventDefault();
    };
    document.addEventListener("click", onLink, true);
    window.addEventListener("beforeunload", onUnload);
    return () => {
      document.removeEventListener("click", onLink, true);
      window.removeEventListener("beforeunload", onUnload);
    };
  }, []);

  if (error) return <div className="banner warn">Settings could not be loaded: {error}</div>;
  if (html === null) return <div className="empty">Loading…</div>;
  return (
    <div className="settings-wrap">
      {tabs.length ? (
        <div className="settings-tabs" role="tablist" aria-label="Settings">
          {tabs.map((t) => (
            <button key={t.key} role="tab" aria-selected={active === t.key} onClick={() => choose(t.key)}>
              {t.label}
              {dirty.has(t.key) ? <span className="unsaved" title="Unsaved changes"> ●</span> : null}
            </button>
          ))}
        </div>
      ) : null}
      {/* Server-rendered by this app with every value escaped. */}
      <div className="settings-page" ref={page} dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}
