import { expect, type APIRequestContext, type Locator, type Page } from "@playwright/test";

export const FB = {
  host: process.env.FB_HOST ?? "127.0.0.1",
  port: Number(process.env.FB_PORT ?? 3050),
  user: process.env.FB_USER ?? "SYSDBA",
  password: process.env.FB_PASSWORD ?? "masterkey",
  dir: process.env.FB_DB_DIR ?? "/var/lib/firebird/data",
};

export const SCHEMA = `
CREATE TABLE CUSTOMER (
  ID INTEGER NOT NULL PRIMARY KEY,
  NAME VARCHAR(50) NOT NULL,
  CITY VARCHAR(30),
  BALANCE NUMERIC(12,2) DEFAULT 0
);
CREATE INDEX IX_CUSTOMER_CITY ON CUSTOMER (CITY);
CREATE VIEW V_RICH AS SELECT ID, NAME FROM CUSTOMER WHERE BALANCE > 100;
INSERT INTO CUSTOMER VALUES (1, 'Ann', 'Bucharest', 150);
INSERT INTO CUSTOMER VALUES (2, 'Bob', 'Lisbon', 20);
INSERT INTO CUSTOMER VALUES (3, 'Cid', 'Prague', 0);
`;

/** Create a fresh database with SCHEMA through the API (using a temporary registration). */
export async function createTestDatabase(request: APIRequestContext): Promise<string> {
  const path = `${FB.dir}/fbadmin_e2e_${Date.now().toString(36)}.fdb`;
  const srv = await (await request.post("/api/servers", {
    data: { name: "e2e-setup", host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json();
  try {
    const db = await request.post(`/api/servers/${srv.id}/databases/create`, { data: { name: "e2e", path } });
    expect(db.status(), await db.text()).toBe(201);
    const dbId = (await db.json()).id;
    const r = await (await request.post(`/api/servers/${srv.id}/databases/${dbId}/execute`, { data: { sql: SCHEMA } })).json();
    expect(r.error, JSON.stringify(r.error)).toBeNull();
  } finally {
    await request.delete(`/api/servers/${srv.id}`);
  }
  return path;
}

/** Drop a database created by createTestDatabase (best effort). */
export async function dropTestDatabase(request: APIRequestContext, path: string) {
  const srv = await (await request.post("/api/servers", {
    data: { name: "e2e-teardown", host: FB.host, port: FB.port, user: FB.user, password: FB.password },
  })).json();
  try {
    const db = await (await request.post(`/api/servers/${srv.id}/databases`, { data: { name: "drop", path } })).json();
    await request.post(`/api/servers/${srv.id}/databases/${db.id}/drop`);
  } finally {
    await request.delete(`/api/servers/${srv.id}`);
  }
}

/** The input that follows a form label (the dialogs use label + input pairs). */
export function field(scope: Page | Locator, label: string): Locator {
  return scope.locator(`.form label.l:text-is("${label}") + *`);
}

export function treeRow(page: Page, label: string): Locator {
  return page.locator(".tree-row", { has: page.locator(`.label:text-is("${label}")`) }).first();
}

/** Expand a tree node (click its twisty) and wait for children or "No items". */
export async function expand(page: Page, label: string) {
  const row = treeRow(page, label);
  await expect(row).toBeVisible();
  await row.locator(".twisty").click();
}

/** Replace the content of the CodeMirror editor inside ``scope``. */
export async function setEditor(page: Page, scope: Locator, sql: string) {
  const content = scope.locator(".cm-content").first();
  await content.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("Delete");
  await page.keyboard.insertText(sql);
}
