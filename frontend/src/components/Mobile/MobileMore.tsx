/**
 * MORE on a phone: engine health, feed settings, audio cues, and — on the
 * paper server only — the strategy BOT monitor (the live server has no
 * strategy plane, so nothing pretends to be one there).
 */

import { useEffect, useState } from "react";
import { cycleAudioMode, getAudioMode, onAudioModeChange } from "../../lib/audio";
import { useTradingMode } from "../../store/accountStore";
import { FeedSettingsPanel, HealthPanel, useSystemState } from "../System/SystemPanels";
import { MobileMonitorSheet } from "./MobileMonitorSheet";

const AUDIO_LABEL = { off: "OFF", fx: "FX (chimes)", vox: "VOX (spoken)" } as const;

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-bb-border">
      <div className="px-3 pb-1 pt-3 text-[10px] tracking-widest text-bb-muted">{title}</div>
      {children}
    </div>
  );
}

export function MobileMore() {
  const state = useSystemState();
  const { live } = useTradingMode();
  const [audio, setAudio] = useState(getAudioMode());
  useEffect(() => onAudioModeChange(setAudio), []);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto overscroll-contain">
      <Section title="ENGINE">
        <HealthPanel state={state} />
      </Section>
      <Section title="TRADE AUDIO">
        <button
          className="mx-3 mb-2 flex h-11 items-center justify-between border border-bb-border px-3 text-[12px]"
          onClick={() => cycleAudioMode()}
        >
          <span className="text-bb-muted">cues on fills, stops, rejections</span>
          <span className={audio === "off" ? "text-bb-muted" : "text-bb-amber"}>{AUDIO_LABEL[audio]}</span>
        </button>
      </Section>
      <Section title="FEED / API">
        <FeedSettingsPanel />
      </Section>
      {!live && (
        <Section title="STRATEGY BOT">
          <MobileMonitorSheet />
        </Section>
      )}
    </div>
  );
}
