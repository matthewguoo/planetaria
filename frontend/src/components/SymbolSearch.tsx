import { useCallback, useEffect, useRef, useState } from "react";
import { searchSymbols, type SymbolHit } from "../lib/api";
import { useTradingStore } from "../store/tradingStore";

/** TradingView-style symbol autocomplete: prefix-ranked liquid names,
 *  arrow-key navigation, Enter to select, Escape to dismiss. */
export function SymbolSearch() {
  const symbol = useTradingStore((s) => s.symbol);
  const setSymbol = useTradingStore((s) => s.setSymbol);

  const [draft, setDraft] = useState<string | null>(null);
  const [hits, setHits] = useState<SymbolHit[]>([]);
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
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

  // Close on outside click.
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) {
        setOpen(false);
        setDraft(null);
      }
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, []);

  const select = (hit: SymbolHit) => {
    setSymbol(hit.symbol);
    setDraft(null);
    setOpen(false);
    inputRef.current?.blur();
  };

  const commitFreeText = () => {
    const cleaned = (draft ?? "").trim().toUpperCase();
    if (cleaned && /^[A-Z.]{1,10}$/.test(cleaned)) setSymbol(cleaned);
    setDraft(null);
    setOpen(false);
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
        if (hits[active]) select(hits[active]);
        else commitFreeText();
        break;
      case "Escape":
        setDraft(null);
        setOpen(false);
        inputRef.current?.blur();
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

  return (
    <div ref={wrapRef} className="relative">
      <input
        ref={inputRef}
        className="w-24 border border-bb-border bg-black px-2 py-0.5 text-bb-amber outline-none focus:border-bb-amber"
        value={draft ?? symbol}
        onChange={(e) => {
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
        aria-label="Symbol"
        placeholder={symbol}
      />
      {open && hits.length > 0 && (
        <ul className="absolute left-0 top-full z-40 mt-px max-h-72 w-64 overflow-y-auto border border-bb-border bg-bb-panel shadow-lg">
          {hits.map((hit, i) => (
            <li
              key={hit.symbol}
              onMouseDown={(e) => {
                e.preventDefault();
                select(hit);
              }}
              onMouseEnter={() => setActive(i)}
              className={
                "flex cursor-pointer items-baseline justify-between gap-3 px-2 py-1 " +
                (i === active ? "bg-bb-hover" : "")
              }
            >
              <span className="font-semibold text-white">{highlight(hit.symbol, q)}</span>
              <span className="truncate text-right text-[11px] text-bb-muted">
                {highlight(hit.name, q)}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
