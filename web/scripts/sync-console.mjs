// Build step: copy the one console page from the Python package so there is a single source of truth,
// and point it at the /backend proxy instead of its own origin.
import { copyFileSync, mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const web = join(dirname(fileURLToPath(import.meta.url)), "..");
const src = join(web, "..", "urban_response", "static", "index.html");
const out = join(web, "public");

mkdirSync(out, { recursive: true });
copyFileSync(src, join(out, "index.html"));
writeFileSync(join(out, "suru-config.js"), 'window.SURU_API_BASE = "/backend";\n');
console.log(`console copied from ${src}`);
