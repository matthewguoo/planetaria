/**
 * PROFILE chips: DEFAULT / SCALP / SWING. One tap writes a named bundle of
 * server-enforced risk rules (and the feed cadences that go with it) —
 * the "good settings for scalping" without hunting through twelve fields.
 * The active chip is whichever preset the stored settings currently EQUAL,
 * so a hand edit afterwards simply un-lights it; nothing is remembered by
 * name. Capability facts (options level, shorts) are never in a preset.
 */

import { useEffect, useState } from "react";
import { apiError, applyRiskPreset, getRiskPresets, type RiskPreset } from "../../lib/api";
import { etTimePlusMinutes } from "../../lib/orderPayload";
import { useAccountStore } from "../../store/accountStore";
import { useStrategyStore } from "../../store/strategyStore";

/** Fired after a preset lands so pollers re-read the feed cadences. */
export const SETTINGS_CHANGED_EVENT = "planetaria:settings-changed";

export function RiskPresetChips({ touch = false }: { touch?: boolean }) {
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const [presets, setPresets] = useState<RiskPreset[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const account = useAccountStore((s) => s.account);

  useEffect(() => {
    let alive = true;
    getRiskPresets()
      .then((r) => {
        if (!alive) return;
        setPresets(r.presets);
        setActive(r.active);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
    // Re-evaluate "active" whenever the account (and its risk block) refreshes.
  }, [account?.risk]);

  const apply = async (name: string) => {
    setBusy(name);
    setMsg(null);
    try {
      const result = await applyRiskPreset(name);
      setActive(name);
      // The ticket follows the profile: exits from its defaults, and a
      // scalp hold if the preset carries one.
      const preset = presets.find((p) => p.name === name);
      const hold = preset?.ticket?.hold_min;
      useStrategyStore.getState().seedExits(
        result.risk.default_tp_pct,
        result.risk.default_sl_pct,
        hold ? etTimePlusMinutes(hold) : undefined,
      );
      await refreshAccount();
      window.dispatchEvent(new Event(SETTINGS_CHANGED_EVENT));
      setMsg(`${name.toUpperCase()} applied`);
    } catch (err) {
      setMsg(`✗ ${apiError(err)}`);
    } finally {
      setBusy(null);
    }
  };

  const chip = (on: boolean) =>
    touch
      ? "h-10 shrink-0 border px-3 text-[12px] tracking-widest " +
        (on ? "border-bb-amber bg-bb-amber font-semibold text-black" : "border-bb-border text-bb-muted active:text-bb-amber")
      : "px-2 py-0.5 text-[10px] tracking-widest " +
        (on ? "bg-bb-amber font-semibold text-black" : "border border-bb-border text-bb-muted hover:text-bb-amber");

  if (!presets.length) return null;
  return (
    <div className="flex flex-col gap-1">
      <div className={"flex items-center gap-1 " + (touch ? "chip-rail" : "")}>
        <span className={"text-bb-muted " + (touch ? "text-[12px]" : "text-[10px] tracking-widest")}>PROFILE</span>
        {presets.map((p) => (
          <button
            key={p.name}
            className={chip(active === p.name)}
            disabled={busy !== null}
            title={p.blurb}
            onClick={() => void apply(p.name)}
          >
            {busy === p.name ? "…" : p.label}
          </button>
        ))}
      </div>
      {msg && (
        <div className={"text-[10px] " + (msg.startsWith("✗") ? "text-bb-loss" : "text-bb-profit")}>{msg}</div>
      )}
    </div>
  );
}
