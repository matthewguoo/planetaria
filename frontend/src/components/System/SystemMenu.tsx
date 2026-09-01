/**
 * SYSTEM drawer (⚙ in the options terminal's header, desktop + phone).
 *
 * The ops console has a whole SYSTEM page instead; this is the cockpit's
 * narrow version of the same thing, and it is deliberately a thin arrangement
 * of the shared panels rather than its own implementation — see
 * SystemPanels.tsx.
 */

import { AccountsPanel, FeedSettingsPanel, HealthPanel, useSystemState } from "./SystemPanels";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <>
      <div className="px-2 pb-1 pt-4 text-[9px] tracking-widest text-bb-muted first:pt-2">
        {title}
      </div>
      {children}
    </>
  );
}

export function SystemMenu({ onClose }: { onClose: () => void }) {
  const state = useSystemState();

  return (
    <>
      <div className="fixed inset-0 z-40 bg-black/50" onClick={onClose} />
      <div className="fixed right-0 top-0 z-50 flex h-full w-full max-w-sm flex-col border-l border-bb-amber/60 bg-bb-panel">
        <div className="flex shrink-0 items-center justify-between border-b border-bb-border px-3 py-2">
          <span className="text-[11px] tracking-widest text-bb-amber">SYSTEM</span>
          <button
            className="px-2 text-[14px] leading-none text-bb-muted hover:text-bb-loss"
            onClick={onClose}
            aria-label="Close system menu"
          >
            ✕
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          <Section title="STATE">
            <HealthPanel state={state} />
          </Section>
          <Section title="PAPER ACCOUNT (restart applies)">
            <AccountsPanel />
          </Section>
          <Section title="FEED / API SETTINGS">
            <FeedSettingsPanel />
          </Section>
        </div>
      </div>
    </>
  );
}
