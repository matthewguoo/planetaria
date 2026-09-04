/**
 * A two-tap button for anything that moves money from a dense desktop
 * surface (the positions drawer, the account table). The phone shell and
 * the position panel already confirm every close and flatten; the tables
 * here fired on the first click, which on the live server is a real order
 * from a mis-click. First click arms and relabels (LIVE named in red when
 * the server is live); second click within ARM_MS acts; anything else
 * disarms.
 */

import { useEffect, useState } from "react";
import { useTradingMode } from "../store/accountStore";

const ARM_MS = 4000;

export function ConfirmButton({
  label, confirmLabel, onConfirm, disabled = false, className = "", title,
}: {
  label: string;
  /** Shown while armed; defaults to `CONFIRM <label>`. LIVE is appended on the live server. */
  confirmLabel?: string;
  onConfirm: () => void | Promise<void>;
  disabled?: boolean;
  className?: string;
  title?: string;
}) {
  const { live } = useTradingMode();
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    if (!armed) return;
    const id = window.setTimeout(() => setArmed(false), ARM_MS);
    return () => window.clearTimeout(id);
  }, [armed]);

  const armedText = `${confirmLabel ?? `CONFIRM ${label}`}${live ? " · LIVE" : ""}`;
  const base = "border px-1.5 text-[10px] disabled:opacity-40 ";
  const idle = "border-bb-loss text-bb-loss hover:bg-bb-loss hover:text-black";
  const hot = "border-bb-loss bg-bb-loss font-semibold text-black";

  return (
    <button
      className={base + (armed ? hot : idle) + " " + className}
      disabled={disabled}
      title={title}
      onBlur={() => setArmed(false)}
      onClick={(e) => {
        e.stopPropagation();
        if (!armed) {
          setArmed(true);
          return;
        }
        setArmed(false);
        void onConfirm();
      }}
    >
      {armed ? armedText : label}
    </button>
  );
}
