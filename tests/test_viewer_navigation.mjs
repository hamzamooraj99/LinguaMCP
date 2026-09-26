import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const navigationSource = await readFile(
  new URL("../viewer/static/navigation.js", import.meta.url),
  "utf8",
);
const { createRouteNavigation } = await import(
  `data:text/javascript;base64,${Buffer.from(navigationSource).toString("base64")}`
);

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

const languages = [{ id: "german", label: "German" }];

function groups(paths, group = "current") {
  const pathList = Array.isArray(paths) ? paths : [paths];
  return [{
    id: group,
    documents: pathList.map((path) => ({ path, label: path, group })),
  }];
}

test("a late route cannot replace the selected document after list results reorder", async () => {
  const firstList = deferred();
  const rendered = [];
  let listCall = 0;
  const dataSource = {
    listDocuments: () => (++listCall === 1 ? firstList.promise : Promise.resolve(groups("02-progress.md"))),
    getDocument: async (_language, path) => ({ path, label: path, html: `<p>${path}</p>` }),
  };
  const navigation = createRouteNavigation(dataSource, (result) => rendered.push(result));

  const oldRoute = navigation.render("german", "01-lesson-plan.md", languages);
  const selectedRoute = navigation.render("german", "02-progress.md", languages);
  await selectedRoute;
  firstList.resolve(groups("01-lesson-plan.md"));
  await oldRoute;

  const final = rendered.at(-1);
  assert.equal(final.kind, "document");
  assert.equal(final.documentData.label, "02-progress.md");
  assert.equal(final.documentData.path, "02-progress.md");
  assert.equal(final.target.path, "02-progress.md");
  assert.equal(final.targetGroup, "current");
  assert.deepEqual(final.groups, groups("02-progress.md"));
  assert.equal(rendered.some((result) => result.kind === "document" && result.documentData.path === "01-lesson-plan.md"), false);
});

test("a stale document error cannot replace a newer route", async () => {
  const oldDocument = deferred();
  const rendered = [];
  const dataSource = {
    listDocuments: async (_language) => groups(["01-lesson-plan.md", "02-progress.md"]),
    getDocument: (_language, path) => path === "01-lesson-plan.md"
      ? oldDocument.promise
      : Promise.resolve({ path, label: "Progress", html: "<p>new content</p>" }),
  };
  const navigation = createRouteNavigation(dataSource, (result) => rendered.push(result));

  const oldRoute = navigation.render("german", "01-lesson-plan.md", languages);
  await Promise.resolve();
  const selectedRoute = navigation.render("german", "02-progress.md", languages);
  await selectedRoute;
  oldDocument.reject(new Error("old route failed"));
  await oldRoute;

  const final = rendered.at(-1);
  assert.equal(final.kind, "document");
  assert.equal(final.documentData.path, "02-progress.md");
  assert.equal(final.documentData.html, "<p>new content</p>");
  assert.equal(rendered.some((result) => result.kind === "error"), false);
});

test("returning to the picker invalidates pending reader work", async () => {
  const oldList = deferred();
  const rendered = [];
  const dataSource = {
    listDocuments: () => oldList.promise,
    getDocument: async (_language, path) => ({ path, label: path, html: path }),
  };
  const navigation = createRouteNavigation(dataSource, (result) => rendered.push(result));

  const oldRoute = navigation.render("german", "02-progress.md", languages);
  await navigation.render(null, null, languages);
  oldList.resolve(groups("02-progress.md"));
  await oldRoute;

  assert.equal(rendered.at(-1).kind, "picker");
  assert.equal(rendered.some((result) => result.kind === "document"), false);
});
