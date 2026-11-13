// Default urban_response API base URL for this deploy. Empty = run the built-in mock (mock.js).
// Served locally (web/run-local.sh on :3100), talk to the DGX Spark through the SSH tunnel on :8080.
// Override per visit with ?api=https://... (remembered in this browser); ?api= with no value forces the mock.
// The Vercel build replaces this file (scripts/sync-console.mjs); FastAPI serves its own /config.js.
window.SURU_API_BASE = ["localhost", "127.0.0.1"].includes(location.hostname) && location.port !== "8080"
  ? "http://localhost:8080" : "";
