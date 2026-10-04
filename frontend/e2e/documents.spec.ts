import { test, expect } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";

// The legacy fitz import writes a warning to stdout, corrupting this PDF stream.
const pdf = execFileSync("python", [
  "-c",
  `import pymupdf,sys
with pymupdf.open() as pdf:
 pdf.new_page().insert_text((72,72), "Document fictif pour la demonstration Instruct IA.")
 pdf.new_page().insert_text((72,72), "Regler la machine DEMO-42 a 12 unites. Procedure fictive.")
 sys.stdout.buffer.write(pdf.tobytes())`,
]);

test("ajout, indexation, question, source exacte et retrait — ordinateur et mobile", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await page.getByLabel("Identifiant", {exact: true}).fill("admin");
  await page.getByLabel("Mot de passe", {exact: true}).fill("Synthetic-password-123");
  await page.getByRole("button", {name: "Se connecter"}).click();
  await page.getByRole("button", {name: "Gestion avancée", exact:true}).click();
  await expect(
    page.getByText(
      "Votre base documentaire est prête à accueillir ses premiers PDF",
    ),
  ).toBeVisible();
  await page.getByLabel("Fichier PDF").setInputFiles({
    name: "fiche-demo.pdf",
    mimeType: "application/pdf",
    buffer: pdf,
  });
  await page.getByLabel("Dossier relatif").fill("maintenance");
  await page
    .getByRole("button", { name: "Enregistrer le PDF", exact: true })
    .click();
  await expect(
    page.getByRole("cell", { name: "À indexer", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Indexer", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "Disponible", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("2 pages", { exact: false })).toBeVisible();
  mkdirSync("../docs/screenshots", { recursive: true });
  await page.screenshot({
    path: "../docs/screenshots/documents-desktop.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    page.getByRole("button", { name: "Remplacer", exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "../docs/screenshots/documents-mobile.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Recherche", exact: true }).click();
  await page
    .getByLabel("Votre question")
    .fill("Quel réglage pour la machine DEMO-42?");
  await page.getByRole("button", { name: "Rechercher", exact: true }).click();
  await expect(
    page
      .locator(".answer")
      .getByText("Regler la machine DEMO-42 a 12 unites. Procedure fictive.", {
        exact: false,
      }),
  ).toBeVisible();
  const versionResponse = page.waitForResponse(
    (r) => r.url().includes("/source?") && r.status() === 200,
  );
  await page
    .getByRole("button", { name: "Ouvrir la source · p. 2", exact: true })
    .click();
  const metadata = await (await versionResponse).json();
  expect(metadata.page).toBe(2);
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("link", { name: "Ouvrir le PDF · page 2", exact: true }),
  ).toHaveAttribute("href", /#page=2$/);
  const download = await dialog
    .getByRole("link", { name: "Télécharger le PDF" })
    .getAttribute("href");
  const file = await page.request.get(download!);
  expect(file.headers()["content-type"]).toContain("application/pdf");
  expect(file.headers()["content-disposition"]).toContain("attachment");
  expect(await file.body()).toEqual(pdf);
  const actualDownload = page.waitForEvent("download");
  await dialog.getByRole("link", { name: "Télécharger le PDF" }).click();
  const downloaded = await actualDownload;
  expect(await downloaded.failure()).toBeNull();
  expect(readFileSync((await downloaded.path())!)).toEqual(pdf);
  await page.screenshot({
    path: "../docs/screenshots/source-mobile.png",
    fullPage: true,
  });
  await dialog.getByRole("button", { name: "Fermer", exact: true }).click();
  await page.getByRole("button", { name: "Gestion avancée", exact: true }).click();
  await page.getByRole("button", { name: "Retirer", exact: true }).click();
  await expect(
    page
      .getByRole("dialog")
      .getByText("Le fichier sera conservé", { exact: false }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Confirmer le retrait", exact: true })
    .click();
  await expect(
    page.getByText(
      "Votre base documentaire est prête à accueillir ses premiers PDF",
    ),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "↻ Synchroniser les documents", exact: true })
    .click();
  await expect(page.getByText("Terminé", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Recherche", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Ouvrir la source · p. 2", exact: true }),
  ).toHaveCount(0);
  await page.getByLabel("Votre question").fill("Quel réglage pour la machine DEMO-42?");
  await page.getByRole("button", { name: "Rechercher", exact: true }).click();
  await expect(
    page.getByText(
      "Information non trouvée dans les instructions disponibles.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(errors).toEqual([]);
});
