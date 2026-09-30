import { expect, test } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdirSync } from "node:fs";

const question = "Quel réglage pour la machine DEMO-42?";
const pdf = execFileSync("python", [
  "-c",
  `import fitz,sys
with fitz.open() as pdf:
 pdf.new_page().insert_text((72,72), "Regler la machine DEMO-42 a 12 unites. Procedure fictive.")
 sys.stdout.buffer.write(pdf.tobytes())`,
]);

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Identifiant", { exact: true }).fill("admin");
  await page
    .getByLabel("Mot de passe", { exact: true })
    .fill("Synthetic-password-123");
  await page.getByRole("button", { name: "Se connecter" }).click();
  await expect(page.getByRole("button", { name: "Déconnexion" })).toBeVisible();
});

test("Rapide → Approfondir, recherche sans génération, citations et mobile", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const session = await (await page.request.get("/api/auth/session")).json();
  const headers = {
    "X-CSRF-Token": session.csrf,
    Origin: "http://127.0.0.1:3000",
  };
  const group = await (
    await page.request.post("/api/admin/groups", {
      headers,
      data: { name: "Modes" },
    })
  ).json();
  const upload = await page.request.post("/api/documents", {
    headers,
    multipart: {
      path: "modes/fiche.pdf",
      groups: String(group.id),
      file: { name: "fiche.pdf", mimeType: "application/pdf", buffer: pdf },
    },
  });
  expect(upload.status()).toBe(201);
  const id = (await upload.json()).id;
  expect(
    (
      await page.request.post(`/api/documents/${id}/sync`, { headers })
    ).status(),
  ).toBe(200);
  // Pick up the new access version before storing any result.
  await page.reload();
  await expect(
    page.getByRole("radio", { name: "Rapide", exact: true }),
  ).toBeChecked();
  await expect(
    page.getByRole("radio", { name: "Réflexion", exact: true }),
  ).toBeEnabled();
  await page.getByLabel("Votre question").fill(question);
  const baseline = await (
    await page.request.get("http://127.0.0.1:8000/__test/metrics")
  ).json();
  await page.getByRole("button", { name: "Rechercher", exact: true }).click();
  await expect(
    page.getByText("Réponse en cours…", { exact: true }),
  ).toBeVisible();
  const first = page.getByRole("region", {
    name: "Réponse · Rapide",
    exact: true,
  });
  await expect(first.locator(".answer")).toContainText("12 unites");
  mkdirSync("../docs/screenshots", { recursive: true });
  await page.screenshot({
    path: "../docs/screenshots/modes-fast-desktop.png",
    fullPage: true,
  });
  let metrics = await (
    await page.request.get("http://127.0.0.1:8000/__test/metrics")
  ).json();
  expect(metrics.chats - baseline.chats).toBe(1);
  expect(metrics.requests.at(-1).think).toBe(false);

  // Editing the textarea must not change the question used by Approfondir.
  await page
    .getByLabel("Votre question")
    .fill("Une nouvelle question non envoyée");
  await page
    .getByRole("button", { name: "Approfondir", exact: true })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
      button.click();
    });
  await expect(
    page.getByText("Réflexion en cours…", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Approfondir", exact: true }),
  ).toBeDisabled();
  await expect(first.locator(".answer")).toContainText("12 unites");
  await page.screenshot({
    path: "../docs/screenshots/modes-reflection-loading.png",
    fullPage: true,
  });
  const deeper = page.getByRole("region", {
    name: "Réponse approfondie · Réflexion",
    exact: true,
  });
  await expect(deeper.locator(".answer")).toContainText("12 unites");
  await expect(first).toBeVisible();
  await expect(
    deeper.getByRole("link", { name: "Citation 1", exact: true }),
  ).toHaveAttribute("href", /^#deeper-source-/);
  metrics = await (
    await page.request.get("http://127.0.0.1:8000/__test/metrics")
  ).json();
  expect(metrics.chats - baseline.chats).toBe(2);
  expect(metrics.embeddings - baseline.embeddings).toBe(2);
  const request = metrics.requests.at(-1);
  expect(request.think).toBe(true);
  expect(request.messages.map((m: { role: string }) => m.role)).toEqual([
    "system",
    "user",
  ]);
  expect(JSON.parse(request.messages[1].content).QUESTION).toBe(question);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../docs/screenshots/modes-deeper-mobile.png",
    fullPage: true,
  });

  // Native radio controls are usable with the keyboard.
  const fast = page.getByRole("radio", { name: "Rapide", exact: true });
  await fast.focus();
  await page.keyboard.press("ArrowRight");
  await expect(
    page.getByRole("radio", { name: "Réflexion", exact: true }),
  ).toBeChecked();
  await page.keyboard.press("ArrowRight");
  await expect(
    page.getByRole("radio", { name: "Recherche seulement", exact: true }),
  ).toBeChecked();
  await page.getByLabel("Votre question").fill(question);
  await page.getByRole("button", { name: "Rechercher", exact: true }).click();
  const search = page.getByRole("region", {
    name: "Résultats documentaires",
    exact: true,
  });
  await expect(
    search.getByText("Ces passages sont des résultats de recherche.", {
      exact: false,
    }),
  ).toBeVisible();
  await expect(
    search.getByRole("link", { name: "modes/fiche.pdf — page 1" }),
  ).toBeVisible();
  metrics = await (
    await page.request.get("http://127.0.0.1:8000/__test/metrics")
  ).json();
  expect(metrics.chats - baseline.chats).toBe(2);
  expect(metrics.embeddings - baseline.embeddings).toBe(3);
  await page.screenshot({
    path: "../docs/screenshots/modes-search-mobile.png",
    fullPage: true,
  });
  await search
    .getByRole("button", { name: "Ouvrir la source · p. 1", exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog")
      .getByRole("link", { name: "Ouvrir le PDF · page 1", exact: true }),
  ).toHaveAttribute("href", /#page=1$/);
  expect(errors).toEqual([]);
});

test("un mode indisponible est expliqué, désactivé et ne propose pas Approfondir", async ({
  page,
}) => {
  await page.route("**/api/response-modes", (route) =>
    route.fulfill({
      json: {
        default: "fast",
        modes: {
          fast: { available: true, reason: "" },
          reflection: {
            available: false,
            reason: "Le modèle ne prend pas en charge Réflexion.",
          },
          search: { available: true, reason: "" },
        },
      },
    }),
  );
  await page.reload();
  await expect(
    page.getByRole("radio", { name: "Réflexion", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByText("Le modèle ne prend pas en charge Réflexion.", {
      exact: true,
    }),
  ).toBeVisible();
  await page.getByLabel("Votre question").fill(question);
  await page.getByRole("button", { name: "Rechercher", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "Réponse · Rapide", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Approfondir", exact: true }),
  ).toHaveCount(0);
});

test("un délai dépassé pendant Approfondir conserve la première réponse", async ({
  page,
}) => {
  await page.getByLabel("Votre question").fill(question);
  await page.getByRole("button", { name: "Rechercher", exact: true }).click();
  const first = page.getByRole("region", {
    name: "Réponse · Rapide",
    exact: true,
  });
  await expect(first).toBeVisible();
  await page.route("**/api/ask", (route) =>
    route.fulfill({
      status: 504,
      json: {
        code: "REQUEST_TIMEOUT",
        detail: "Délai maximal dépassé. Aucune réponse complète disponible.",
      },
    }),
  );
  await page.getByRole("button", { name: "Approfondir", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Délai maximal dépassé");
  await expect(first).toBeVisible();
  await expect(
    page.getByRole("region", {
      name: "Réponse approfondie · Réflexion",
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Approfondir", exact: true }),
  ).toBeEnabled();
});
