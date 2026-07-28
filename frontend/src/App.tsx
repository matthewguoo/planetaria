import { useEffect, useState } from "react";
import { Header } from "./components/Header";
import { CandlePane } from "./components/Chart/CandlePane";
import { ChartControls } from "./components/Chart/ChartControls";

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
        <div className="text-lg tracking-widest text-bb-amber">PLANETARIA</div>
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

// ?unlock bypasses the viewport lock (QA/testing in small panes).
const UNLOCKED = new URLSearchParams(window.location.search).has("unlock");

export default function App() {
  const locked = useViewportLock();
  if (locked && !UNLOCKED) return <ViewportLockout />;

  return (
    <div className="flex h-full flex-col gap-px bg-bb-black p-px">
      <Header />

      <main className="panel flex min-h-0 flex-1 flex-col">
        <ChartControls />
        <div className="min-h-0 flex-1">
          <CandlePane />
        </div>
      </main>

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
