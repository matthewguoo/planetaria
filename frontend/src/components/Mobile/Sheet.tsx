/**
 * Bottom sheet for the phone layout: slides over the chart, backdrop tap,
 * ✕, or a downward swipe on the grab handle closes it. The chart stays
 * visible (and live) above the sheet. Heights are viewport fractions so a
 * short phone in landscape still leaves the header reachable.
 */

import { useRef, useState } from "react";

const SWIPE_CLOSE_PX = 72;

export function Sheet({
  title,
  onClose,
  children,
  tall = false,
  right,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  /** Nearly full height (tickets, account); default caps at 70dvh. */
  tall?: boolean;
  /** Optional control rendered in the header's right slot (before ✕). */
  right?: React.ReactNode;
}) {
  const [dy, setDy] = useState(0);
  const startRef = useRef<number | null>(null);

  const onDown = (e: React.PointerEvent) => {
    startRef.current = e.clientY;
    try {
      (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    } catch {
      /* synthetic events */
    }
  };
  const onMove = (e: React.PointerEvent) => {
    if (startRef.current === null) return;
    setDy(Math.max(0, e.clientY - startRef.current));
  };
  const onUp = () => {
    if (startRef.current === null) return;
    const shouldClose = dy > SWIPE_CLOSE_PX;
    startRef.current = null;
    setDy(0);
    if (shouldClose) onClose();
  };

  return (
    <>
      <div className="fixed inset-0 z-30 bg-black/50" onClick={onClose} />
      <div
        className={
          "fixed inset-x-0 bottom-0 z-40 flex flex-col border-t border-bb-amber/60 bg-bb-panel " +
          "pb-[env(safe-area-inset-bottom)] " +
          (tall ? "h-[90dvh]" : "max-h-[70dvh]")
        }
        style={dy ? { transform: `translateY(${dy}px)`, transition: "none" } : undefined}
        role="dialog"
        aria-label={title}
      >
        {/* Grab handle + title row is the swipe surface. */}
        <div
          className="touch-none flex shrink-0 select-none flex-col border-b border-bb-border"
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerCancel={onUp}
        >
          <div className="mx-auto mt-1.5 h-1 w-10 bg-bb-border" aria-hidden />
          <div className="flex h-11 items-center justify-between pl-3 pr-1">
            <span className="text-[12px] tracking-widest text-bb-amber">{title}</span>
            <span className="flex items-center gap-1">
              {right}
              <button
                className="h-11 w-11 text-[18px] leading-none text-bb-muted active:text-bb-loss"
                onClick={onClose}
                aria-label="Close sheet"
              >
                ✕
              </button>
            </span>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">{children}</div>
      </div>
    </>
  );
}
