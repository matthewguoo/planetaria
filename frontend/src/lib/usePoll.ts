import { useEffect, useRef } from "react";

/**
 * Run `fn` now and on every `ms` tick — the house polling pattern with the
 * unmount race handled once. The `alive` probe passed to `fn` answers "does
 * this effect still own the screen?"; an async body must check it before
 * setState. `deps` restarts the cycle (with an immediate call) when refetch
 * inputs change — e.g. the equity-curve period. Eight hand-rolled copies of
 * this block existed; three of them forgot the guard.
 */
export function usePoll(
  fn: (alive: () => boolean) => void | Promise<void>,
  ms: number,
  deps: unknown[] = [],
) {
  const fnRef = useRef(fn);
  useEffect(() => {
    fnRef.current = fn;
  });
  useEffect(() => {
    let alive = true;
    const probe = () => alive;
    const tick = () => void fnRef.current(probe);
    tick();
    const id = window.setInterval(tick, ms);
    return () => {
      alive = false;
      window.clearInterval(id);
    };
    // The spread is the point: callers declare their refetch inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, ...deps]);
}
