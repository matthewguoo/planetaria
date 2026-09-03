/**
 * ACCOUNT CAPABILITIES — the persistent declaration of what this account
 * is approved for. Saved into the risk settings (server-enforced at entry),
 * read by every pane through lib/capabilities so unsupported shapes are
 * not shown: an IRA at level 2 with no shorting never sees a spread
 * preset, a sell button on the chain or a SHORT side. On the live server
 * the options level is floored at 2 whatever is stored.
 */

import { useState } from "react";
import { apiError, putRisk } from "../../lib/api";
import { OPTIONS_LEVEL_LABEL, useCapabilities } from "../../lib/capabilities";
import { useAccountStore } from "../../store/accountStore";

const LEVELS = [0, 2, 3] as const;

export function CapabilitiesPanel({ touch = false }: { touch?: boolean }) {
  const risk = useAccountStore((s) => s.account?.risk);
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const caps = useCapabilities();
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!risk) return <div className="px-2 py-2 text-[11px] text-bb-muted">loading…</div>;

  const save = async (patch: Parameters<typeof putRisk>[0], key: string) => {
    setSaving(key);
    setError(null);
    try {
      await putRisk(patch);
      await refreshAccount();
    } catch (err) {
      setError(apiError(err));
    } finally {
      setSaving(null);
    }
  };

  const h = touch ? "h-11" : "h-7";
  const txt = touch ? "text-[12px]" : "text-[10px]";
  const seg = (on: boolean, disabled = false) =>
    `${h} flex-1 border px-2 ${txt} tracking-wider disabled:opacity-40 ` +
    (on ? "border-bb-amber bg-bb-amber font-semibold text-black" : "border-bb-border text-bb-muted hover:text-bb-amber active:text-bb-amber") +
    (disabled ? " cursor-not-allowed" : "");

  const toggle = (label: string, hint: string, on: boolean, onChange: (v: boolean) => void, key: string) => (
    <div className="flex items-center justify-between gap-3 py-1.5">
      <span className="flex min-w-0 flex-col">
        <span className={`${txt} tracking-wider text-bb-muted`}>{label}</span>
        <span className={"text-bb-muted " + (touch ? "text-[11px]" : "text-[9px]")}>{hint}</span>
      </span>
      <span className="flex shrink-0 gap-px">
        <button className={seg(!on)} disabled={saving === key} onClick={() => onChange(false)}>OFF</button>
        <button className={seg(on)} disabled={saving === key} onClick={() => onChange(true)}>ON</button>
      </span>
    </div>
  );

  return (
    <div className={"flex flex-col " + (touch ? "gap-1 px-3 py-2" : "gap-0.5 px-2 py-1")}>
      <div className="py-1.5">
        <div className="flex items-baseline justify-between">
          <span className={`${txt} tracking-wider text-bb-muted`}>OPTIONS LEVEL</span>
          <span className={"text-bb-muted " + (touch ? "text-[11px]" : "text-[9px]")}>
            {caps.live ? "live server floors this at 2" : "what the broker approved"}
          </span>
        </div>
        <div className="mt-1 flex gap-px">
          {LEVELS.map((lvl) => (
            <button
              key={lvl}
              className={seg(risk.options_level === lvl, caps.live && lvl > 2)}
              disabled={saving === "options_level" || (caps.live && lvl > 2)}
              onClick={() => save({ options_level: lvl }, "options_level")}
              title={OPTIONS_LEVEL_LABEL[lvl]}
            >
              {lvl === 0 ? "NONE" : `L${lvl}`}
            </button>
          ))}
        </div>
        <div className={"mt-1 text-bb-muted " + (touch ? "text-[11px]" : "text-[9px]")}>
          {OPTIONS_LEVEL_LABEL[caps.optionsLevel]}
        </div>
      </div>
      {toggle(
        "EQUITY SHORTS",
        "off = long-only book (cash / IRA). On needs margin + the broker's borrow flags.",
        !risk.equity_long_only,
        (v) => save({ equity_long_only: !v }, "equity_long_only"),
        "equity_long_only",
      )}
      {!risk.equity_long_only &&
        toggle(
          "SHORTS OVERNIGHT",
          "new short entries 20:00–04:00 ET on delayed tape — keep off",
          risk.equity_short_overnight,
          (v) => save({ equity_short_overnight: v }, "equity_short_overnight"),
          "equity_short_overnight",
        )}
      {toggle(
        "STOP REQUIRED ON SHARES",
        "manual share entries without a stop are refused",
        risk.manual_equity_require_stop,
        (v) => save({ manual_equity_require_stop: v }, "manual_equity_require_stop"),
        "manual_equity_require_stop",
      )}
      {error && <div className="text-[11px] text-bb-loss">✗ {error}</div>}
    </div>
  );
}
