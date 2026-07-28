import axios from "axios";

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL ?? "http://localhost:8000",
  timeout: 10_000,
});

export type SymbolHit = { symbol: string; name: string };

export async function searchSymbols(q: string): Promise<SymbolHit[]> {
  const { data } = await api.get<{ results: SymbolHit[] }>("/api/symbols/search", {
    params: { q },
  });
  return data.results;
}
