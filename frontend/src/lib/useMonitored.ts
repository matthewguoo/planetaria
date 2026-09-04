import { useState } from "react";
import { getSystemState } from "./api";
import { usePoll } from "./usePoll";

/** Plan ids the exit enforcer currently has a monitor task for; null until
 * the first system-state fetch (or while the backend is unreachable). */
export function useMonitored(): Set<string> | null {
  const [ids, setIds] = useState<Set<string> | null>(null);
  usePoll(async (alive) => {
    try {
      const sys = await getSystemState();
      if (alive()) setIds(new Set(sys.enforcer.monitored_plan_ids));
    } catch {
      if (alive()) setIds(null);
    }
  }, 10_000);
  return ids;
}
