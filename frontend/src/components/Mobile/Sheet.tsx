/** Bottom sheet for the phone layout: slides over the chart, backdrop tap
 * or ✕ closes. The chart stays visible (and live) above the sheet. */

export function Sheet({
  title,
  onClose,
  children,
  tall = false,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  tall?: boolean;
}) {
  return (
    <>
      <div className="fixed inset-0 z-30 bg-black/40" onClick={onClose} />
      <div
        className={
          "fixed inset-x-0 bottom-0 z-40 flex flex-col border-t border-bb-amber/60 bg-bb-panel " +
          (tall ? "h-[82dvh]" : "max-h-[68dvh]")
        }
      >
        <div className="flex shrink-0 items-center justify-between border-b border-bb-border px-3 py-1.5">
          <span className="text-[11px] tracking-widest text-bb-amber">{title}</span>
          <button
            className="px-2 py-0.5 text-[14px] leading-none text-bb-muted active:text-bb-loss"
            onClick={onClose}
            aria-label="Close sheet"
          >
            ✕
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </div>
    </>
  );
}
