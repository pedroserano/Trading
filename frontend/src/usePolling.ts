import { useEffect } from "react";

/** Runs `load` now and every `ms` while the page is visible. It stops while the app is in the
 * background and loads once on return, so a phone in a pocket or a hidden tab costs nothing.
 *
 * `load` gets `current()`: false once the page has moved on (another account or symbol, or
 * the screen closed), so a slow answer for the old one is dropped. Pass a `useCallback`-ed
 * `load`; when it changes, polling restarts with the new one. */
export function usePolling(load: (current: () => boolean) => unknown, ms: number): void {
  useEffect(() => {
    let alive = true;
    let timer: number | undefined;
    const current = () => alive;
    const tick = () => void load(current);
    const start = () => {
      if (timer === undefined) timer = window.setInterval(tick, ms);
    };
    const stop = () => {
      if (timer !== undefined) window.clearInterval(timer);
      timer = undefined;
    };
    const onVisibility = () => {
      if (document.hidden) {
        stop();
      } else {
        tick();
        start();
      }
    };
    tick();
    if (!document.hidden) start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      alive = false;
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [load, ms]);
}
