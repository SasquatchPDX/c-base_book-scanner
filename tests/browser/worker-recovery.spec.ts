import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";

test("an owner acknowledges a failed worker without losing the original", async ({ page }) => {
  await page.goto("/accounts/login/");
  await page.getByLabel("LDAP username").fill("leela");
  await page.getByLabel("Password").fill("leela");
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.goto("/reviews/?view=html");
  await page.getByRole("combobox", { name: "Task type", exact: true }).selectOption("worker_failure");
  await page.getByRole("button", { name: "Apply filters" }).click();
  await page.getByRole("link", { name: /Browser Worker Recovery/ }).click();
  await expect(page.getByRole("button", { name: "Retry job", exact: true })).toBeEnabled();
  await expect(page.getByRole("heading", { name: "Attempt 1 · failed" })).toBeVisible();
  await page.getByRole("button", { name: "Acknowledge failure", exact: true }).click();
  await expect(page.locator(".status")).toHaveText("Resolved");
  await page.getByRole("link", { name: "Worker failure queue" }).click();
  await expect(page.getByRole("link", { name: /Browser Worker Recovery/ })).toHaveCount(0);

  await page.goto("/library/books/?view=html&q=Browser%20Worker%20Recovery");
  await page.getByRole("link", { name: "Browser Worker Recovery", exact: true }).click();
  const variant = page.locator(".variant-list > div").filter({ hasText: "worker-recovery-original.pdf" });
  const downloading = page.waitForEvent("download");
  await variant.getByRole("link", { name: "Download", exact: true }).click();
  const download = await downloading;
  expect(download.suggestedFilename()).toBe("worker-recovery-original.pdf");
  const path = await download.path();
  expect(path).not.toBeNull();
  expect(await readFile(path!, "utf8")).toBe("%PDF-1.4\nBrowser recovery original\n");
});

