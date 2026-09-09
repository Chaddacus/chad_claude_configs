import { expect, test } from "@playwright/test";

test("Rubik smoke path", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByTestId("cube-viewport")).toBeVisible();
  await expect(page.getByTestId("cube-status")).toHaveText("solved");

  await page.getByRole("button", { name: "Scramble" }).click();
  await expect(page.getByTestId("cube-status")).toHaveText("scrambled");

  await page.getByRole("button", { name: "Solve" }).click();
  await expect(page.getByTestId("cube-status")).toHaveText("solved");

  await page.getByRole("button", { name: "Reset" }).click();
  await expect(page.getByTestId("cube-status")).toHaveText("solved");
});
