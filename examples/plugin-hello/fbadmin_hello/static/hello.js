// Frontend half of the example plugin. Receives the fbadmin SDK (see docs/PLUGINS.md).
export default function setup(fb) {
  const h = fb.React.createElement;

  function HelloTab({ tab }) {
    const [n, setN] = fb.React.useState(null);
    fb.React.useEffect(() => {
      fb.request("GET", `/api/hello/${tab.serverId}/${tab.dbId}/table-count`).then((r) => setN(r.tables));
    }, []);
    return h("div", { className: "pad" }, h("h2", null, "Hello from a plugin"),
      h("p", null, n === null ? "Counting…" : `This database has ${n} user tables.`));
  }

  fb.registry.registerTabKind("hello", { render: (tab) => h(HelloTab, { tab }) });
  fb.registry.registerMenu((app, sel) =>
    sel.kind === "database"
      ? [{ label: "Hello plugin", onClick: () => app.openTab({ kind: "hello", title: "Hello", serverId: sel.serverId, dbId: sel.dbId }) }]
      : []);
}
