/**
 * ACCOUNT CAPABILITIES — what this account can actually do, verified at
 * the broker. The options level is read-only here: it is whatever the
 * probe proved (or the broker reports), and the server caps the stored
 * risk settings at it. The PROBE button places the smallest real orders
 * that can prove or refute each capability — a 1-share round trip, a
 * non-marketable short attempt, a far-OTM long option, a spread, a short
 * put — records the broker's verbatim answer, and cancels/flattens. On the
 * live server it is red and needs "LIVE" typed; it never runs on its own.
 */

import { useState } from "react";
import {
  abortProbe,
  apiError,
  applyCapabilities,
  getCapabilities,
  probeCapabilities,
  putRisk,
  refreshCapabilities,
  type CapabilitiesStatus,
  type CapabilityCheck,
} from "../../lib/api";
import { OPTIONS_LEVEL_LABEL, useCapabilities } from "../../lib/capabilities";
import { usePoll } from "../../lib/usePoll";
import { useAccountStore } from "../../store/accountStore";
import { Dot } from "../System/SystemPanels";

const OPTION_CHECKS = ["option_l2_long", "option_l3_spread", "option_short_put", "option_naked_call", "option_l1_covered"];

function statusDot(status: string) {
  return <Dot ok={status === "PASS"} warn={status === "SKIP" || status === "INFO" || status === "RUNNING"} />;
}

function fmtWhen(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d.getTime())
    ? ""
    : d.toLocaleString("en-US", { timeZone: "America/New_York", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) + " ET";
}

function CheckRow({ row, touch }: { row: CapabilityCheck; touch: boolean }) {
  const [open, setOpen] = useState(false);
  const cls = row.status === "FAIL" ? "text-bb-loss" : row.status === "PASS" ? "text-bb-profit" : "text-bb-muted";
  return (
    <div className="border-b border-bb-border/40">
      <button
        className={"flex w-full items-center gap-2 px-2 text-left " + (touch ? "min-h-10 py-1.5" : "py-1")}
        onClick={() => setOpen(!open)}
        title={row.detail}
      >
        {statusDot(row.status)}
        <span className={"w-36 shrink-0 tracking-wider text-bb-muted " + (touch ? "text-[11px]" : "text-[10px]")}>
          {row.name}
        </span>
        <span className={"min-w-0 flex-1 truncate " + (touch ? "text-[11px] " : "text-[10px] ") + cls}>
          {row.status === "RUNNING" ? "…" : `${row.status} · ${row.detail}`}
        </span>
      </button>
      {open && row.detail && (
        <pre className="whitespace-pre-wrap break-words border-t border-bb-border/30 bg-black/40 px-2 py-1 text-[10px] text-white">
          {row.detail}
        </pre>
      )}
    </div>
  );
}

export function CapabilitiesPanel({ touch = false }: { touch?: boolean }) {
  const risk = useAccountStore((s) => s.account?.risk);
  const account = useAccountStore((s) => s.account);
  const refreshAccount = useAccountStore((s) => s.refreshAccount);
  const caps = useCapabilities();
  const [status, setStatus] = useState<CapabilitiesStatus | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState("");
  const [showChecks, setShowChecks] = useState(false);

  const running = status?.running ?? false;
  usePoll(async (alive) => {
    try {
      const s = await getCapabilities();
      if (!alive()) return;
      setStatus(s);
    } catch {
      /* transient */
    }
  }, running ? 1000 : 30_000, [running]);

  if (!risk) return <div className="px-2 py-2 text-[11px] text-bb-muted">…</div>;

  const act = async (key: string, fn: () => Promise<unknown>) => {
    setSaving(key);
    setError(null);
    try {
      await fn();
      const s = await getCapabilities();
      setStatus(s);
      await refreshAccount();
    } catch (err) {
      setError(apiError(err));
    } finally {
      setSaving(null);
    }
  };

  const h = touch ? "h-11" : "h-7";
  const txt = touch ? "text-[12px]" : "text-[10px]";
  const hint = touch ? "text-[11px]" : "text-[9px]";
  const seg = (on: boolean, disabled = false) =>
    `${h} flex-1 border px-2 ${txt} tracking-wider disabled:opacity-40 ` +
    (on ? "border-bb-amber bg-bb-amber font-semibold text-black" : "border-bb-border text-bb-muted hover:text-bb-amber active:text-bb-amber") +
    (disabled ? " cursor-not-allowed" : "");

  const toggle = (label: string, on: boolean, onChange: (v: boolean) => void, key: string, disabledWhy?: string) => (
    <div className="flex items-center justify-between gap-3 py-1.5" title={disabledWhy}>
      <span className="flex min-w-0 flex-col">
        <span className={`${txt} tracking-wider text-bb-muted`}>{label}</span>
        {disabledWhy && <span className={hint + " text-bb-muted"}>{disabledWhy}</span>}
      </span>
      <span className="flex shrink-0 gap-px">
        <button className={seg(!on, !!disabledWhy)} disabled={saving === key || !!disabledWhy} onClick={() => onChange(false)}>OFF</button>
        <button className={seg(on, !!disabledWhy)} disabled={saving === key || !!disabledWhy} onClick={() => onChange(true)}>ON</button>
      </span>
    </div>
  );

  const derived = status?.derived ?? account?.capabilities?.derived ?? {};
  const broker = status?.broker ?? {};
  const level = risk.options_level;
  const provenance = status?.level_provenance ?? account?.capabilities?.level_provenance ?? "";
  const checks = status?.checks ?? [];
  const counts = checks.reduce<Record<string, number>>((acc, c) => ({ ...acc, [c.status]: (acc[c.status] ?? 0) + 1 }), {});
  const optionSkips = checks.some((c) => OPTION_CHECKS.includes(c.name) && c.status === "SKIP");
  const liveOk = !caps.live || confirm === "LIVE";
  const shortsRefused = derived.equity_shorts === false && status?.sources?.equity_shorts !== "default";
  const brokerLine = [
    derived.cash_account === true ? "cash" : derived.cash_account === false ? "margin" : null,
    broker.shorting_enabled === false || broker.config?.no_shorting ? "no shorting" : broker.shorting_enabled ? "shorting" : null,
    broker.config?.fractional_trading ? "fractional" : null,
    broker.options_approved_level != null ? `L${broker.options_approved_level}/${broker.options_trading_level ?? "?"}` : null,
    broker.pattern_day_trader ? "PDT" : null,
  ].filter(Boolean).join(" · ");

  return (
    <div className={"flex flex-col " + (touch ? "gap-1 px-3 py-2" : "gap-0.5 px-2 py-1")}>
      {status?.manual_action && (
        <div className="border border-bb-loss bg-bb-loss/10 px-2 py-1.5 text-[11px] text-bb-loss">
          ⚠ {status.manual_action}
        </div>
      )}

      <div className="py-1.5">
        <div className="flex items-baseline justify-between gap-2">
          <span className={`${txt} tracking-wider text-bb-muted`}>OPTIONS LEVEL</span>
          <span data-numeric className={"text-white " + (touch ? "text-[13px]" : "text-[11px]")}>
            L{level}
            <span className={"ml-2 text-bb-muted " + hint}>{provenance}</span>
          </span>
        </div>
        <div className={"mt-0.5 text-bb-muted " + hint}>{OPTIONS_LEVEL_LABEL[level]}</div>
        {brokerLine && (
          <div className={"mt-0.5 flex items-center justify-between text-bb-muted " + hint}>
            <span>broker: {brokerLine}</span>
            <button
              className="px-1 text-bb-muted hover:text-bb-amber"
              disabled={saving === "refresh"}
              title="Re-read the broker's account flags (no orders)"
              onClick={() => act("refresh", refreshCapabilities)}
            >
              ↻
            </button>
          </div>
        )}
        {status?.apply_pending && (
          <button
            className={`${h} mt-1 w-full border border-bb-amber px-2 ${txt} tracking-wider text-bb-amber hover:bg-bb-amber hover:text-black disabled:opacity-40`}
            disabled={saving === "apply"}
            title="The probe verified more than the stored risk settings allow — widen them to match"
            onClick={() => act("apply", applyCapabilities)}
          >
            {saving === "apply" ? "…" : "APPLY VERIFIED CAPABILITIES TO RISK RULES"}
          </button>
        )}
      </div>

      {toggle(
        "EQUITY SHORTS",
        !risk.equity_long_only,
        (v) => act("equity_long_only", () => putRisk({ equity_long_only: !v })),
        "equity_long_only",
        shortsRefused ? "shorting refused at the broker" : undefined,
      )}
      {!risk.equity_long_only &&
        toggle(
          "SHORTS OVERNIGHT",
          risk.equity_short_overnight,
          (v) => act("equity_short_overnight", () => putRisk({ equity_short_overnight: v })),
          "equity_short_overnight",
        )}
      {toggle(
        "STOP REQUIRED ON SHARES",
        risk.manual_equity_require_stop,
        (v) => act("manual_equity_require_stop", () => putRisk({ manual_equity_require_stop: v })),
        "manual_equity_require_stop",
      )}

      <div className="mt-1 border-t border-bb-border/40 pt-1.5">
        <div className="flex items-center justify-between gap-2">
          <span className={`${txt} tracking-wider text-bb-muted`}>
            PROBE {status?.probed_at ? `· ${fmtWhen(status.probed_at)}` : "· never run"}
            {status?.probe_session ? ` · ${status.probe_session}` : ""}
          </span>
          {checks.length > 0 && !running && (
            <button className={hint + " text-bb-muted hover:text-bb-amber"} onClick={() => setShowChecks(!showChecks)}>
              {counts.PASS ?? 0} PASS · {counts.FAIL ?? 0} FAIL · {counts.SKIP ?? 0} SKIP {showChecks ? "▴" : "▾"}
            </button>
          )}
        </div>
        {running && status && (
          <div className={"mt-1 flex items-center justify-between " + txt}>
            <span className="text-bb-amber">
              PROBING… {status.progress.done}/{status.progress.total} {status.progress.current ?? ""}
            </span>
            <button className="border border-bb-loss px-2 text-bb-loss" onClick={() => act("abort", abortProbe)}>ABORT</button>
          </div>
        )}
        {!running && (
          <div className="mt-1 flex items-center gap-1">
            {caps.live && (
              <input
                className={`${h} w-20 border border-bb-loss bg-black px-2 ${txt} text-bb-loss outline-none placeholder:text-bb-loss/40`}
                placeholder="type LIVE"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value.toUpperCase())}
                aria-label="Type LIVE to enable the live probe"
              />
            )}
            <button
              className={
                `${h} flex-1 border px-2 ${txt} tracking-wider disabled:opacity-40 ` +
                (caps.live
                  ? "border-bb-loss bg-bb-loss/20 text-bb-loss hover:bg-bb-loss hover:text-black"
                  : "border-bb-amber text-bb-amber hover:bg-bb-amber hover:text-black")
              }
              disabled={saving === "probe" || !liveOk}
              title="Places the smallest real orders that prove each capability (1-share round trip, a short attempt, a far-OTM option, a spread), records the broker's verbatim answer, cancels and flattens everything"
              onClick={() => act("probe", () => probeCapabilities({ confirm: caps.live ? confirm : undefined }))}
            >
              {saving === "probe" ? "…" : caps.live ? "PROBE CAPABILITIES (LIVE · REAL ORDERS)" : "PROBE CAPABILITIES"}
            </button>
            {optionSkips && (
              <button
                className={`${h} border border-bb-border px-2 ${txt} tracking-wider text-bb-muted hover:text-bb-amber disabled:opacity-40`}
                disabled={saving === "probe" || !liveOk}
                title="The option checks were skipped outside RTH — run just those now"
                onClick={() => act("probe", () => probeCapabilities({ confirm: caps.live ? confirm : undefined, only: OPTION_CHECKS }))}
              >
                RE-RUN OPTIONS
              </button>
            )}
          </div>
        )}
        {(running || showChecks) && checks.length > 0 && (
          <div className="mt-1 max-h-[50dvh] overflow-y-auto border border-bb-border/40">
            {checks.map((row, i) => (
              <CheckRow key={`${row.name}-${i}`} row={row} touch={touch} />
            ))}
          </div>
        )}
      </div>
      {error && <div className="text-[11px] text-bb-loss">✗ {error}</div>}
    </div>
  );
}
