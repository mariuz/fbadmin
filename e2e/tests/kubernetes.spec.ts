import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page, type Route } from "@playwright/test";

/**
 * Roadmap 2.2: the Kubernetes page (FirebirdClusters of the cloudnative-firebird operator). The
 * Kubernetes API is not available here, so /api/kubernetes is answered by the browser: this checks
 * the page and the requests it sends. The backend side runs against a fake API server
 * (backend/tests/test_kubernetes.py) and against the real operator on kind (k8s-operator.yml).
 */
test.describe.configure({ mode: "serial" });

let page: Page;
const calls: Array<{ method: string; path: string; body: unknown }> = [];

const NS = "prod";
const cluster = {
  namespace: NS, name: "fbcluster", created: 1_760_000_000, phase: "Running", phase_reason: null, instances: 2, ready: 2,
  image: "firebirdsql/firebird:5", primary: "fbcluster-0", replication: true, replication_mode: "async",
  hibernated: false, suspended: false, database: "demo.fdb", fenced: [] as string[], superuser_secret: "fbcluster-superuser",
  backup_s3: false, switchover_to: null, registered_server_id: null,
};
const detail = () => ({
  ...cluster,
  conditions: [{ type: "Ready", status: "True", reason: null, message: "all instances ready" }],
  pods: [
    { name: "fbcluster-0", role: "primary", ready: true, phase: "Running", node: "node-a", restarts: 0, started: 1_760_000_100, fenced: false },
    { name: "fbcluster-1", role: "replica", ready: true, phase: "Running", node: "node-b", restarts: 1, started: 1_760_000_200, fenced: cluster.fenced.includes("fbcluster-1") },
  ],
  backups: [{ name: "fbcluster-nightly-1", type: "logical", level: null, database: null, phase: "Completed", started: 1_760_001_000,
              completed: 1_760_001_042, location: "/var/lib/firebird/data/fbcluster-nightly-1.fbk", verified: true, error: null }],
  scheduled_backups: [{ name: "nightly", schedule: "0 2 * * *", type: "logical", suspended: false, last_successful: 1_760_001_042, last_backup: "fbcluster-nightly-1" }],
  databases: [{ name: "sales", file: "sales.fdb", phase: "Ready", path: "/var/lib/firebird/data/sales.fdb" }],
  users: [{ name: "app", username: "APP", admin: false, phase: "Ready" }],
  events: [{ type: "Normal", reason: "BackupCompleted", message: "fbcluster-nightly-1 completed", count: 1, time: 1_760_001_042 }],
});

async function kube(route: Route) {
  const req = route.request();
  const path = new URL(req.url()).pathname.replace("/api/kubernetes", "");
  const body = req.postData() ? JSON.parse(req.postData()!) : null;
  if (req.method() !== "GET") calls.push({ method: req.method(), path, body });
  const json = (data: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(data) });
  const base = `/clusters/${NS}/fbcluster`;
  if (path === "/status") return json({ enabled: true, source: "service account", server: "https://10.96.0.1:443", namespaces: [NS], operator: true });
  if (path === "/clusters") return json([cluster]);
  if (path === base) return json(detail());
  if (path === base + "/logs") return json({ pod: "fbcluster-1", container: "firebird", text: "Firebird 5.0.3 started\nreplica connected" });
  if (path === base + "/hibernate") { cluster.hibernated = body.on; return json({ message: `Cluster fbcluster ${body.on ? "hibernating" : "resuming"}` }); }
  if (path === base + "/fence") { cluster.fenced = body.on ? [body.instance] : []; return json({ message: "fenced", fenced: cluster.fenced }); }
  if (path === base + "/switchover") return json({ message: `Switchover of cluster fbcluster requested: fbcluster-0 -> ${body.target}` });
  if (path === base + "/backups") return json({ message: "FirebirdBackup fbcluster-1 created (logical)", name: "fbcluster-1" }, 201);
  if (path === base + "/restores") return json({ message: "FirebirdRestore created", name: "r1", file: body.target }, 201);
  if (path === base + "/scale") return json({ message: `Cluster fbcluster: ${body.instances} instance(s) requested` });
  if (path === base + "/register") {
    cluster.registered_server_id = "k8s-prod-fbcluster" as never;
    return json({ server_id: "k8s-prod-fbcluster", host: "fbcluster.prod.svc", databases: ["/var/lib/firebird/data/demo.fdb"], created: true, password_from_secret: true });
  }
  return json({ detail: "not mocked: " + path }, 404);
}

test.beforeAll(async ({ browser }) => {
  page = await (await browser.newContext()).newPage();
  await page.route("**/api/kubernetes/**", kube);
  page.on("dialog", (d) => d.accept());
  await page.goto("/");
});

test.afterAll(async () => {
  await page?.context().close();
});

const pane = () => page.locator(".tab-pane:not(.hidden)");
const last = () => calls[calls.length - 1];

async function audit(what: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).exclude(".cm-editor").analyze();
  expect(r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`), what).toEqual([]);
}

test("clusters, instances, backups, events; accessible in every theme", async () => {
  await page.locator(".topbar").getByRole("button", { name: "Kubernetes", exact: true }).click();
  // the extra topbar button must not push the bar's last buttons out of the window
  const bar = await page.locator(".topbar").evaluate((el) => ({ scroll: el.scrollWidth, client: el.clientWidth }));
  expect(bar.scroll, "topbar overflows").toBeLessThanOrEqual(bar.client);
  await expect(pane()).toContainText("service account, API server https://10.96.0.1:443; namespaces: prod");
  await expect(pane().locator("table.grid").first().locator("tbody tr")).toContainText("fbcluster");
  const view = pane().locator(".mt-section", { hasText: "Cluster fbcluster" });
  await expect(view).toContainText("2 of 2 ready");
  await expect(view).toContainText("async");
  const pods = pane().locator(".mt-section", { hasText: "Restarts" });
  await expect(pods.locator("tbody tr")).toHaveCount(2);
  // only the replica can be promoted
  await expect(pods.locator("tbody tr", { hasText: "fbcluster-0" }).getByRole("button", { name: "Promote" })).toHaveCount(0);
  await expect(pane()).toContainText("fbcluster-nightly-1");
  await expect(pane()).toContainText("0 2 * * *");
  await expect(pane()).toContainText("BackupCompleted");
  await expect(pane()).toContainText("sales");
  for (const theme of ["light", "dark", "contrast"]) {
    await page.evaluate((th) => { document.documentElement.dataset.theme = th; }, theme);
    await audit(`Kubernetes page (${theme} theme)`);
  }
  await page.evaluate(() => { delete document.documentElement.dataset.theme; });
});

test("actions send the operator's requests", async () => {
  const pods = pane().locator(".mt-section", { hasText: "Restarts" });
  const replica = pods.locator("tbody tr", { hasText: "fbcluster-1" });

  await replica.getByRole("button", { name: "Logs" }).click();
  await expect(pane().getByLabel("Logs of fbcluster-1")).toContainText("replica connected");

  await replica.getByRole("button", { name: "Promote" }).click();
  await expect.poll(() => last()).toEqual({ method: "POST", path: `/clusters/${NS}/fbcluster/switchover`, body: { target: "fbcluster-1" } });

  await replica.getByRole("button", { name: "Fence" }).click();
  await expect.poll(() => last()?.body).toEqual({ on: true, instance: "fbcluster-1" });
  await expect(replica).toContainText("(fenced)");
  await replica.getByRole("button", { name: "Unfence" }).click();
  await expect.poll(() => last()?.body).toEqual({ on: false, instance: "fbcluster-1" });

  await pane().getByLabel("Backup type").selectOption("physical");
  await pane().getByRole("button", { name: "Back up now" }).click();
  await expect.poll(() => last()?.body).toEqual({ type: "physical", level: 0 });

  await pane().getByLabel("Restore").selectOption("fbcluster-nightly-1");
  await pane().getByLabel("Into the file").fill("check.fdb");
  await pane().locator(".k8s-restore").getByRole("button", { name: "Restore" }).click();
  await expect.poll(() => last()).toEqual({ method: "POST", path: `/clusters/${NS}/fbcluster/restores`, body: { backup: "fbcluster-nightly-1", target: "check.fdb" } });

  await pane().getByLabel("Instances", { exact: true }).fill("3");
  await pane().getByRole("button", { name: "Scale" }).click();
  await expect.poll(() => last()?.body).toEqual({ instances: 3 });

  await pane().getByRole("button", { name: "Register in fbadmin…" }).click();
  await audit("register form");
  await pane().getByRole("button", { name: "Register in fbadmin", exact: true }).click();
  await expect.poll(() => last()).toEqual({ method: "POST", path: `/clusters/${NS}/fbcluster/register`, body: { host: null, port: 3050, password: null, shared: false } });
  await expect(pane().getByRole("button", { name: "Show in the Object Explorer" })).toBeVisible();

  await pane().getByRole("button", { name: "Hibernate" }).click();
  await expect.poll(() => last()?.body).toEqual({ on: true });
  await expect(pane().getByRole("button", { name: "Resume" })).toBeVisible();
  await expect(pane().getByRole("button", { name: "Rolling restart" })).toBeDisabled();
});
