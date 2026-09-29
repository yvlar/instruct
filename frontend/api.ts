export const API = import.meta.env.VITE_API_URL ?? "http://localhost:8000";

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, init);
  if (!response.ok) {
    let detail = "Le service local est indisponible. Réessayez.";
    try {
      const body = await response.json();
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* response may not be JSON */
    }
    throw new Error(detail);
  }
  return response.json();
}

export function message(error: unknown) {
  return error instanceof Error ? error.message : "Une erreur est survenue.";
}
