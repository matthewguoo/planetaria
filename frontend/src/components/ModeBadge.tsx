import { useTradingMode } from "../store/accountStore";

/**
 * The one place the UI says which server it is on. Amber PAPER is the
 * historical badge; red LIVE means every ticket on this page spends real
 * money. Derived from the account poll (Account.mode), never hardcoded —
 * the same build serves both servers.
 */
export function ModeBadge() {
  const { live, loaded } = useTradingMode();
  if (live) {
    return (
      <span
        className="border border-bb-loss bg-bb-loss px-2 py-0.5 font-semibold tracking-widest text-black"
        title="LIVE SERVER — real money. Manual entries only; the strategy plane does not exist in this process. Exits are still enforced server-side."
      >
        LIVE
      </span>
    );
  }
  return (
    <span
      className="border border-bb-border px-2 py-0.5 text-bb-orange"
      title={loaded ? "Paper server — simulated fills, no real money" : "reading server mode…"}
    >
      PAPER
    </span>
  );
}
