import { useEffect, useState } from "react";

const MIN_WIDTH = 1280;

function useViewportLock(): boolean {
  const [locked, setLocked] = useState(window.innerWidth < MIN_WIDTH);
  useEffect(() => {
    const onResize = () => setLocked(window.innerWidth < MIN_WIDTH);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  return locked;
}

function ViewportLockout() {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black">
      <div className="panel max-w-md p-8 text-center">
        <div className="text-bb-amber text-lg tracking-widest">PLANETARIA</div>
        <div className="mt-4 text-bb-muted">
          This terminal requires a desktop viewport of at least {MIN_WIDTH}px.
        </div>
        <div className="mt-2 text-bb-muted">
          Current width: <span className="text-bb-amber">{window.innerWidth}px</span>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const locked = useViewportLock();
  if (locked) return <ViewportLockout />;

  return (
    <div className="flex h-full flex-col gap-px bg-bb-black p-px">
      {/* Top bar — ticker / account summary (placeholder until data layer lands) */}
      <header className="panel flex h-9 shrink-0 items-center gap-6 px-3">
        <span className="tracking-widest text-bb-amber">PLANETARIA</span>
        <span className="text-bb-muted">SPY</span>
        <span className="ml-auto text-bb-muted">PAPER</span>
      </header>

      {/* Chart area */}
      <main className="panel min-h-0 flex-1">
        <div className="flex h-full items-center justify-center text-bb-muted">
          CHART — awaiting data layer
        </div>
      </main>

      {/* Bottom panel row */}
      <section className="grid h-56 shrink-0 grid-cols-4 gap-px">
        {["STRATEGY", "SIZING", "PROBABILITY", "ORDER"].map((title) => (
          <div key={title} className="panel flex flex-col">
            <div className="panel-title">{title}</div>
            <div className="flex flex-1 items-center justify-center text-bb-muted">—</div>
          </div>
        ))}
      </section>
    </div>
  );
}
