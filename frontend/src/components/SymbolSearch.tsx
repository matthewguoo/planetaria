/**
 * Symbol picker — the same one everywhere (desktop chart bar, phone header,
 * any future pane), shaped like TradingView's: a search field that opens a
 * results list of symbol · name · exchange with recent picks on top, arrow /
 * Enter / Escape on a keyboard, one tap on a phone (full-screen sheet with
 * a big input there).
 *
 * Protection is the point, not decoration: every row carries the BROKER's
 * flags. Not tradable → greyed, unpickable, says why. Options mode and no
 * listed options → same. Free-typed tickers are resolved against the broker
 * before they are accepted, so "SPX" or a typo never reaches a chart, a
 * chain fetch or an order.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { apiError, getSymbolInfo, searchSymbols, type SymbolHit } from "../lib/api";
import { useTradingStore } from "../store/tradingStore";

const RECENTS_KEY = "planetaria.recentSymbols";
const RECENTS_MAX = 8;

function loadRecents(): string[] {
  try {
    const raw = localStorage.getItem(RECENTS_KEY);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(arr) ? arr.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function pushRecent(symbol: string): void {
  try {
    const next = [symbol, ...loadRecents().filter((s) => s !== symbol)].slice(0, RECENTS_MAX);
    localStorage.setItem(RECENTS_KEY, JSON.stringify(next));
  } catch {
    /* storage unavailable */
  }
}

/** Why a hit cannot be picked right now, or null when it can. */
export function blockReason(hit: SymbolHit, optionsMode: boolean): string | null {
  if (hit.tradable === false) return "not tradable at Alpaca";
  if (optionsMode && hit.options === false) return "no listed options";
  return null;
}

function Flags({ hit }: { hit: SymbolHit }) {
  return (
    <span className="flex shrink-0 items-center gap-1 text-[10px] tracking-wider">
      {hit.exchange && <span className="text-bb-muted">{hit.exchange}</span>}
      {hit.options === true && <span className="border border-bb-border px-1 text-bb-profit">OPT</span>}
      {hit.shortable === false && hit.tradable !== false && (
        <span className="border border-bb-border px-1 text-bb-muted">NO SHORT</span>
      )}
      {hit.tradable === null && <span className="text-bb-muted" title="broker flags not loaded yet">?</span>}
    </span>
  );
}

export function SymbolSearch({ variant = "inline" }: { variant?: "inline" | "sheet" }) {
  const symbol = useTradingStore((s) => s.symbol);
  const setSymbol = useTradingStore((s) => s.setSymbol);
  const optionsMode = useTradingStore((s) => s.assetMode) === "options";

  const [draft, setDraft] = useState<string | null>(null);
  const [hits, setHits] = useState<SymbolHit[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [resolving, setResolving] = useState(false);
  const debounceRef = useRef<number>(0);
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const query = useCallback((text: string) => {
    window.clearTimeout(debounceRef.current);
    debounceRef.current = window.setTimeout(() => {
      searchSymbols(text)
        .then((results) => {
          setHits(results);
          setActive(0);
        })
        .catch(() => setHits([]));
    }, 120);
  }, []);

  const close = useCallback(() => {
    setOpen(false);
    setDraft(null);
    setError(null);
    inputRef.current?.blur();
  }, []);

  // Desktop dropdown: close on outside click. The sheet has its own scrim.
  useEffect(() => {
    if (variant !== "inline") return;
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) close();
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [variant, close]);

  const select = (hit: SymbolHit) => {
    const why = blockReason(hit, optionsMode);
    if (why) {
      setError(`${hit.symbol}: ${why}`);
      return;
    }
    setSymbol(hit.symbol);
    pushRecent(hit.symbol);
    close();
  };

  /** Free text: only a broker-confirmed, tradable asset gets through. */
  const commitFreeText = async () => {
    const cleaned = (draft ?? "").trim().toUpperCase();
    if (!cleaned) return close();
    if (!/^[A-Z.]{1,10}$/.test(cleaned)) {
      setError(`"${cleaned}" is not a ticker`);
      return;
    }
    const exact = hits.find((h) => h.symbol === cleaned);
    if (exact && exact.tradable !== null) return select(exact);
    setResolving(true);
    try {
      select(await getSymbolInfo(cleaned));
    } catch (err) {
      setError(
        (err as { response?: { status?: number } }).response?.status === 404
          ? `${cleaned} is not a tradable asset at Alpaca`
          : apiError(err),
      );
    } finally {
      setResolving(false);
    }
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!open) return;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        setActive((a) => Math.min(a + 1, hits.length - 1));
        break;
      case "ArrowUp":
        e.preventDefault();
        setActive((a) => Math.max(a - 1, 0));
        break;
      case "Enter":
        e.preventDefault();
        if (hits[active] && (draft ?? "").trim() === "") select(hits[active]);
        else if (hits[active] && hits[active].symbol === (draft ?? "").trim().toUpperCase()) select(hits[active]);
        else if (hits[active] && (draft ?? "").length && active > 0) select(hits[active]);
        else void commitFreeText();
        break;
      case "Escape":
        close();
        break;
    }
  };

  const highlight = (text: string, q: string) => {
    const idx = text.toUpperCase().indexOf(q.toUpperCase());
    if (idx < 0 || !q) return text;
    return (
      <>
        {text.slice(0, idx)}
        <span className="text-bb-amber">{text.slice(idx, idx + q.length)}</span>
        {text.slice(idx + q.length)}
      </>
    );
  };

  const q = draft ?? "";
  const recents = q === "" ? loadRecents().filter((s) => s !== symbol) : [];
  const sheet = variant === "sheet";

  const rows = (
    <ul
      className={
        sheet
          ? "flex-1 overflow-y-auto"
          : "absolute left-0 top-full z-40 mt-px max-h-80 w-80 overflow-y-auto border border-bb-border bg-bb-panel shadow-lg"
      }
      role="listbox"
    >
      {recents.length > 0 && (
        <li className="px-3 pt-2 text-[10px] tracking-widest text-bb-muted">RECENT</li>
      )}
      {recents.map((s) => (
        <li
          key={`r-${s}`}
          role="option"
          aria-selected={false}
          onMouseDown={(e) => {
            e.preventDefault();
            setSymbol(s);
            pushRecent(s);
            close();
          }}
          className={
            "flex cursor-pointer items-center gap-3 px-3 text-white " +
            (sheet ? "h-12 border-b border-bb-border/40 text-[15px]" : "h-8 text-[12px]")
          }
        >
          <span className="text-bb-muted">↻</span>
          <span className="font-semibold">{s}</span>
        </li>
      ))}
      {hits.length > 0 && q === "" && (
        <li className="px-3 pt-2 text-[10px] tracking-widest text-bb-muted">LIQUID NAMES</li>
      )}
      {hits.map((hit, i) => {
        const why = blockReason(hit, optionsMode);
        return (
          <li
            key={hit.symbol}
            role="option"
            aria-selected={i === active}
            aria-disabled={!!why}
            onMouseDown={(e) => {
              e.preventDefault();
              select(hit);
            }}
            onMouseEnter={() => setActive(i)}
            title={why ?? undefined}
            className={
              "flex cursor-pointer items-center gap-3 px-3 " +
              (sheet ? "h-14 border-b border-bb-border/40" : "h-8") +
              (i === active && !sheet ? " bg-bb-hover" : "") +
              (why ? " opacity-40" : "")
            }
          >
            <span className={"w-16 shrink-0 font-semibold text-white " + (sheet ? "text-[16px]" : "text-[12px]")}>
              {highlight(hit.symbol, q)}
            </span>
            <span className="min-w-0 flex-1">
              <span className={"block truncate text-bb-muted " + (sheet ? "text-[12px]" : "text-[11px]")}>
                {highlight(hit.name, q)}
              </span>
              {why && <span className="block text-[10px] text-bb-loss">{why}</span>}
            </span>
            <Flags hit={hit} />
          </li>
        );
      })}
      {q !== "" && hits.length === 0 && (
        <li className="px-3 py-3 text-[12px] text-bb-muted">
          no match — Enter checks “{q.toUpperCase()}” with the broker
        </li>
      )}
    </ul>
  );

  const input = (
    <input
      ref={inputRef}
      className={
        sheet
          ? "h-12 w-full border border-bb-border bg-black px-3 text-[18px] font-semibold text-bb-amber outline-none focus:border-bb-amber"
          : "w-24 border border-bb-border bg-black px-2 py-0.5 text-bb-amber outline-none focus:border-bb-amber"
      }
      value={draft ?? symbol}
      onChange={(e) => {
        setError(null);
        setDraft(e.target.value);
        query(e.target.value);
        setOpen(true);
      }}
      onFocus={(e) => {
        setDraft("");
        query("");
        setOpen(true);
        e.target.select();
      }}
      onKeyDown={onKeyDown}
      spellCheck={false}
      autoCapitalize="characters"
      autoCorrect="off"
      enterKeyHint="go"
      aria-label="Symbol"
      placeholder={sheet ? "Search symbol or company" : symbol}
    />
  );

  if (sheet) {
    return (
      <div ref={wrapRef} className="relative">
        <button
          className="flex h-11 items-center gap-1 px-2 text-[18px] font-semibold text-white active:text-bb-amber"
          onClick={() => {
            setOpen(true);
            setDraft("");
            query("");
            window.setTimeout(() => inputRef.current?.focus(), 30);
          }}
          aria-label="Change symbol"
        >
          {symbol}
          <span className="text-[12px] text-bb-muted">▾</span>
        </button>
        {open && (
          <div className="fixed inset-0 z-50 flex flex-col bg-bb-panel">
            <div className="flex shrink-0 items-center gap-2 border-b border-bb-border p-2">
              <div className="min-w-0 flex-1">{input}</div>
              <button className="h-12 px-3 text-[12px] tracking-widest text-bb-muted" onClick={close}>
                CANCEL
              </button>
            </div>
            {(error || resolving) && (
              <div className={"px-3 py-2 text-[12px] " + (error ? "text-bb-loss" : "text-bb-muted")}>
                {resolving ? "checking with the broker…" : `✗ ${error}`}
              </div>
            )}
            {rows}
            <div className="shrink-0 border-t border-bb-border px-3 py-2 text-[10px] text-bb-muted">
              greyed rows are not tradable on this account · OPT = options listed
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <div ref={wrapRef} className="relative">
      {input}
      {open && (hits.length > 0 || recents.length > 0 || q !== "") && rows}
      {open && (error || resolving) && (
        <div className={"absolute left-0 top-full z-50 mt-px w-80 border border-bb-border bg-black px-2 py-1 text-[11px] " + (error ? "text-bb-loss" : "text-bb-muted")}
          style={{ marginTop: hits.length || recents.length ? undefined : 1 }}>
          {resolving ? "checking with the broker…" : `✗ ${error}`}
        </div>
      )}
    </div>
  );
}
