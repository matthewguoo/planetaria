/**
 * Header for the trading terminal shell (/terminal.html). The ops console
 * has its own Header.tsx (engine-health StatusPill, six tabs); this one
 * carries the cockpit concerns — symbol + live price, the WS feed status,
 * audio cues, and the ⚙ system drawer (the console has a whole SYSTEM page;
 * the drawer is the cockpit's version of it).
 */

import { useEffect, useState } from "react";
import { cycleAudioMode, getAudioMode, onAudioModeChange } from "../lib/audio";
import { ModeBadge } from "./ModeBadge";
import { SystemMenu } from "./System/SystemMenu";
import { useTradingStore } from "../store/tradingStore";
import { PriceReadout } from "./PriceReadout";
import { useUiStore, type View } from "../store/uiStore";

const AUDIO_LABEL = { off: "🔇 OFF", fx: "🔊 FX", vox: "🔊 VOX" } as const;

function AudioToggle() {
  const [mode, setMode] = useState(getAudioMode());
  useEffect(() => onAudioModeChange(setMode), []);
  return (
    <button
      onClick={() => cycleAudioMode()}
      title={
        "Trade audio: OFF · FX (chimes) · VOX (chimes + spoken announcements " +
        "like 'order filled', 'stop loss'). Click anywhere once to unlock browser audio."
      }
      className={
        "px-1.5 py-0.5 text-[11px] tracking-wider " +
        (mode === "off" ? "text-bb-muted hover:text-bb-amber" : "text-bb-amber")
      }
    >
      {AUDIO_LABEL[mode]}
    </button>
  );
}

function StatusPill() {
  const status = useTradingStore((s) => s.status);
  const symbol = useTradingStore((s) => s.symbol);
  let label = "LIVE";
  let cls = "text-bb-profit";
  let title = "Real-time Alpaca market data";
  if (status.connection !== "open") {
    label = status.connection === "connecting" ? "CONNECTING" : "DISCONNECTED";
    cls = "text-bb-loss";
    title = "Feed socket is not connected";
  } else if (status.demo) {
    // Keyless mode: real public prices when the public feed is reachable,
    // synthetic random walk only as a last resort. Never tradeable.
    if (status.sources[symbol] === "public") {
      label = "PUBLIC DATA";
      cls = "text-bb-amber";
      title =
        "Real prices from a keyless public feed (Yahoo, ~5s poll). " +
        "Options chain is modeled from this spot. Set Alpaca keys in .env for tradeable data.";
    } else {
      label = "DEMO DATA";
      cls = "text-bb-neutral";
      title =
        "Synthetic random-walk prices — public feed unreachable and no Alpaca keys. " +
        "NOT real market data.";
    }
  } else if (!status.configured) {
    label = "NO KEYS";
    cls = "text-bb-loss";
    title = "Alpaca keys missing";
  } else if (status.stream_age_s !== null && status.stream_age_s > 30) {
    label = `STALE ${Math.floor(status.stream_age_s)}s`;
    cls = "text-bb-orange";
    title = "No stream message for a while — quotes may be stale";
  }
  return (
    <span className={`${cls} font-semibold`} title={title}>
      {label}
    </span>
  );
}

function SystemButton() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        className="px-1 text-[13px] text-bb-muted hover:text-bb-amber"
        onClick={() => setOpen(true)}
        title="System state + feed/API settings"
        aria-label="System menu"
      >
        ⚙
      </button>
      {open && <SystemMenu onClose={() => setOpen(false)} />}
    </>
  );
}

const TABS: View[] = ["overview", "terminal", "account", "strategies"];

export function TerminalHeader() {
  const symbol = useTradingStore((s) => s.symbol);
  const view = useUiStore((s) => s.view);
  const setView = useUiStore((s) => s.setView);

  return (
    <header className="panel flex h-9 shrink-0 items-center gap-6 px-3">
      <span className="tracking-widest text-bb-amber">PLANETARIA</span>
      <span className="text-white">{symbol}</span>
      <PriceReadout />
      <div className="ml-auto flex items-center gap-4">
        {TABS.map((v) => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={
              "px-2 py-0.5 text-[11px] tracking-widest " +
              (view === v
                ? "bg-bb-amber font-semibold text-black"
                : "text-bb-muted hover:text-bb-amber")
            }
          >
            {v.toUpperCase()}
          </button>
        ))}
        <AudioToggle />
        <SystemButton />
        <StatusPill />
        <ModeBadge />
      </div>
    </header>
  );
}
