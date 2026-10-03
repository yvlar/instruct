export const API = "";

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const session = await fetch("/api/auth/session", { credentials: "same-origin", cache: "no-store" }).then(r => r.json());
  const response = await fetch(`${API}${path}`, { ...init, credentials: "same-origin", cache: "no-store", headers: { ...init?.headers, "X-CSRF-Token": session.csrf } });
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
