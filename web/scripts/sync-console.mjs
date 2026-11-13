// Build step for Vercel. Publishes both UIs into public/, pointed at the /backend proxy:
//   /          SURU Command demo dashboard (web/*.html|js|css, Severian0's UI)
//   /console/  the engine console, copied from the Python package so there is one source of truth
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(web, "public");

mkdirSync(join(out, "scenarios"), { recursive: true });
mkdirSync(join(out, "console"), { recursive: true });

for (const f of ["index.html", "app.js", "api.js", "mock.js", "style.css", "scenarios/sheffield_demo.json"]) {
  copyFileSync(join(web, f), join(out, f));
}
// Live by default on Vercel; ?api= (empty) on a visit still forces the in-browser mock.
writeFileSync(join(out, "config.js"), 'window.SURU_API_BASE = location.origin + "/backend";\n');

const consoleSrc = join(web, "..", "urban_response", "static", "index.html");
copyFileSync(consoleSrc, join(out, "console", "index.html"));
// The console loads a relative suru-config.js: /console resolves it at the root, /console/ inside the folder.
for (const dir of [out, join(out, "console")]) writeFileSync(join(dir, "suru-config.js"), 'window.SURU_API_BASE = "/backend";\n');

console.log(`dashboard + console published to ${out}`);
