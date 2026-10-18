# Plugins

fbadmin can be extended without forking it. A plugin can have a backend part (Python), a frontend part (JavaScript), or both.

## Backend plugins (Python)

Publish a package that registers an object in the `fbadmin.plugins` entry point group:

```toml
[project.entry-points."fbadmin.plugins"]
hello = "fbadmin_hello:plugin"
```

The object can provide any of these attributes:

| Attribute | Meaning |
|---|---|
| `router` | A FastAPI `APIRouter` to include. Use `fbadmin.api.deps.get_state()` to reach the connection manager (`manager.meta(server_id, db_id)`). |
| `setup(app)` | Called once with the FastAPI app. Use it for middleware or startup hooks. |
| `static_dir` | A directory served at `/plugins/<name>/`. |
| `frontend_entry` | A JS module inside `static_dir` that the web UI loads at start-up. |
| `version` | Shown in `GET /api/plugins`. |

A plugin that fails to import is reported in `GET /api/plugins` with an `error` field and does not stop fbadmin.

The AI assistant has its own extension point: providers in the `fbadmin.ai_providers` entry point group (see QUERY_TOOL.md).

### Disabling plugins

Start fbadmin with `FBADMIN_PLUGINS=off` to load no plugins. Use it as a safe mode when a plugin breaks the app.

## Frontend plugins (JavaScript)

There are two ways to add one:
- ship it inside a backend plugin (`static_dir` + `frontend_entry`), or
- drop a `.js` file into `<data dir>/plugins/` (default `~/.fbadmin/plugins/`).

The module's default export is called with the SDK before the UI renders:

```js
export default function setup(fb) {
  const h = fb.React.createElement;
  fb.registry.registerMenu((app, sel) => sel.kind === "database"
    ? [{ label: "My tool", onClick: () => app.openTab({ kind: "my-tool", title: "My tool", serverId: sel.serverId, dbId: sel.dbId }) }]
    : []);
  fb.registry.registerTabKind("my-tool", { render: (tab, visible) => h("div", { className: "pad" }, "Hello") });
}
```

The SDK (`window.fbadmin`) provides:

| Member | Use |
|---|---|
| `registry.registerTabKind(kind, {render, icon?, same?})` | Workspace tab types, opened with `app.openTab({kind, ...})`. |
| `registry.registerMenu((app, selection) => items)` | Context-menu items in the Object Explorer. |
| `registry.registerTopbar({id, render(app)})` | Buttons in the top bar. |
| `registry.registerObjectTab({id, title, applies(sel), render(sel, detail)})` | Extra tabs next to Properties / SQL / Dependencies. |
| `registry.registerDashboardSection({id, render(sel)})` | Sections on the database dashboard. |
| `registry.registerTreeNodes({parent: "database", build(serverId, dbId)})` | Extra nodes in the tree under a database. |
| `api`, `request(method, url, body?)` | The built-in API client, and a raw JSON request helper that handles the password prompt. |
| `React` | The same React instance the UI uses. |
| `t(text)`, `registerCatalog(code, name, messages)` | i18n: translate, or add a language. |
| `getPrefs()` | The user's preferences. |

A complete example lives in `examples/plugin-hello/`: an API endpoint, a database menu item, and a tab. Install it with `pip install -e examples/plugin-hello` and restart fbadmin.
