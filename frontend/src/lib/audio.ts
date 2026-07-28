/**
 * Trading audio cues, MetaTrader/tastytrade style: WebAudio chimes for
 * instant feedback plus spoken announcements ("Order filled") for the
 * lifecycle moments that matter when you're not staring at the screen.
 *
 * Voice clips are local synthesized WAVs (public/audio/*, espeak-ng) — no
 * external requests, no licensing. Modes: off | fx (chimes) | vox (chimes +
 * voice), persisted in localStorage. Browsers block audio until a user
 * gesture; the first pointer/key event unlocks the context, cues before
 * that are silently dropped.
 */

export type AudioMode = "off" | "fx" | "vox";
export type Cue =
  | "submitted"
  | "fill"
  | "partial"
  | "rejected"
  | "cancelled"
  | "tp"
  | "sl"
  | "timeStop"
  | "closedProfit"
  | "closedLoss"
  | "connLost"
  | "connBack";

const STORAGE_KEY = "planetaria-audio-mode";
const MODES: AudioMode[] = ["off", "fx", "vox"];

const VOICE: Record<Cue, string> = {
  submitted: "/audio/order-submitted.wav",
  fill: "/audio/order-filled.wav",
  partial: "/audio/partial-fill.wav",
  rejected: "/audio/order-rejected.wav",
  cancelled: "/audio/order-cancelled.wav",
  tp: "/audio/take-profit.wav",
  sl: "/audio/stop-loss.wav",
  timeStop: "/audio/time-stop.wav",
  closedProfit: "/audio/position-closed.wav",
  closedLoss: "/audio/position-closed.wav",
  connLost: "/audio/connection-lost.wav",
  connBack: "/audio/connection-restored.wav",
};

// (freq Hz, start offset s, duration s) triplets per cue — small motifs with
// distinct shapes so the ear separates profit/loss/alarm without looking.
const MOTIFS: Record<Cue, { notes: [number, number, number][]; type: OscillatorType; gain: number }> = {
  submitted: { notes: [[880, 0, 0.06]], type: "sine", gain: 0.12 },
  fill: { notes: [[740, 0, 0.09], [988, 0.1, 0.14]], type: "triangle", gain: 0.22 },
  partial: { notes: [[740, 0, 0.09]], type: "triangle", gain: 0.18 },
  rejected: { notes: [[150, 0, 0.22]], type: "square", gain: 0.16 },
  cancelled: { notes: [[440, 0, 0.08]], type: "sine", gain: 0.14 },
  tp: { notes: [[523, 0, 0.08], [659, 0.09, 0.08], [784, 0.18, 0.08], [1047, 0.27, 0.18]], type: "triangle", gain: 0.22 },
  sl: { notes: [[440, 0, 0.12], [330, 0.13, 0.12], [220, 0.26, 0.2]], type: "sawtooth", gain: 0.18 },
  timeStop: { notes: [[587, 0, 0.1], [587, 0.16, 0.1]], type: "square", gain: 0.12 },
  closedProfit: { notes: [[659, 0, 0.09], [880, 0.1, 0.16]], type: "triangle", gain: 0.2 },
  closedLoss: { notes: [[494, 0, 0.09], [370, 0.1, 0.16]], type: "triangle", gain: 0.2 },
  connLost: { notes: [[220, 0, 0.12], [180, 0.14, 0.18]], type: "square", gain: 0.14 },
  connBack: { notes: [[440, 0, 0.08], [660, 0.09, 0.12]], type: "sine", gain: 0.14 },
};

let mode: AudioMode = "off";
let ctx: AudioContext | null = null;
let unlocked = false;
const modeListeners = new Set<(m: AudioMode) => void>();

if (typeof window !== "undefined") {
  const stored = window.localStorage?.getItem(STORAGE_KEY) as AudioMode | null;
  mode = stored && MODES.includes(stored) ? stored : "vox";
  const unlock = () => {
    unlocked = true;
    if (!ctx) {
      try {
        ctx = new AudioContext();
      } catch {
        ctx = null;
      }
    }
    void ctx?.resume();
    window.removeEventListener("pointerdown", unlock);
    window.removeEventListener("keydown", unlock);
  };
  window.addEventListener("pointerdown", unlock);
  window.addEventListener("keydown", unlock);
}

export function getAudioMode(): AudioMode {
  return mode;
}

export function cycleAudioMode(): AudioMode {
  mode = MODES[(MODES.indexOf(mode) + 1) % MODES.length];
  if (typeof window !== "undefined") window.localStorage?.setItem(STORAGE_KEY, mode);
  for (const fn of modeListeners) fn(mode);
  playCue(mode === "vox" ? "fill" : "cancelled"); // audible confirmation
  return mode;
}

export function onAudioModeChange(fn: (m: AudioMode) => void): () => void {
  modeListeners.add(fn);
  return () => modeListeners.delete(fn);
}

function chime(cue: Cue): void {
  if (!ctx || ctx.state !== "running") return;
  const motif = MOTIFS[cue];
  const t0 = ctx.currentTime + 0.01;
  for (const [freq, start, dur] of motif.notes) {
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = motif.type;
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(motif.gain, t0 + start);
    gain.gain.exponentialRampToValueAtTime(0.001, t0 + start + dur);
    osc.connect(gain).connect(ctx.destination);
    osc.start(t0 + start);
    osc.stop(t0 + start + dur + 0.02);
  }
}

export function playCue(cue: Cue): void {
  if (mode === "off" || !unlocked) return;
  chime(cue);
  if (mode === "vox") {
    // Voice trails the chime slightly so they read as one event.
    window.setTimeout(() => {
      const audio = new Audio(VOICE[cue]);
      audio.volume = 0.9;
      void audio.play().catch(() => {});
    }, 180);
  }
}

// ------------------------------------------------------- plan transitions

type PlanLike = {
  id: string;
  status: string;
  exit_reason?: string | null;
  realized_pnl?: number | null;
};

const lastStatus = new Map<string, string>();

/**
 * Pure mapping: (previous status, incoming plan) -> cue. Field-only updates
 * (same status) and history replays (first sight of an already-terminal
 * plan) are silent by construction.
 */
export function cueForTransition(prev: string | undefined, plan: PlanLike): Cue | null {
  if (prev === plan.status) return null;
  switch (plan.status) {
    case "submitted":
      return "submitted";
    case "partially_filled":
      return "partial";
    case "filled":
      return "fill";
    case "exiting":
      if (plan.exit_reason === "tp") return "tp";
      if (plan.exit_reason === "sl") return "sl";
      if (plan.exit_reason === "time_stop") return "timeStop";
      return null; // manual close: the user just clicked it
    case "closed":
      if (prev === undefined) return null;
      return plan.realized_pnl != null && plan.realized_pnl < 0 ? "closedLoss" : "closedProfit";
    case "rejected":
      return prev === undefined ? null : "rejected";
    case "cancelled":
      return prev === undefined ? null : "cancelled";
    default:
      return null;
  }
}

/** Snapshot: learn current statuses WITHOUT announcing them (no replay). */
export function primePlans(plans: PlanLike[]): void {
  for (const plan of plans) lastStatus.set(plan.id, plan.status);
}

/** Live plan push: cue on genuine transitions. */
export function onPlanUpdate(plan: PlanLike): void {
  const cue = cueForTransition(lastStatus.get(plan.id), plan);
  lastStatus.set(plan.id, plan.status);
  if (cue) playCue(cue);
}

// ------------------------------------------------------------- connection

let lastConn: string | null = null;

export function onConnectionState(state: string): void {
  if (lastConn === "open" && state === "down") playCue("connLost");
  if (lastConn === "down" && state === "open") playCue("connBack");
  lastConn = state;
}
