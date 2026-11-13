// Vercel Function: forwards /backend/* from the browser to the FastAPI server on the DGX Spark.
//
// Browser ──HTTPS──► Vercel (/backend/...) ──► this function ──HTTPS + Bearer token──► tunnel ──► Spark :8080
//
// The token lives only in Vercel env vars, never in the browser. SSE (text/event-stream) is
// streamed through unbuffered so the agent pipeline still appears live.
//
// Env (Vercel project settings):
//   SURU_BACKEND_URL    public tunnel URL of the Spark, e.g. https://spark.tailnet-1234.ts.net
//   SURU_BACKEND_TOKEN  same value as UR_API_TOKEN on the Spark

const BACKEND = (process.env.SURU_BACKEND_URL || "").replace(/\/+$/, "");
const TOKEN = process.env.SURU_BACKEND_TOKEN || "";

// Only the routes the console uses. Keeps /docs, /openapi.json etc. off the public site.
const ALLOWED = [
  /^api\/(config|scenarios|state|run|reset|burst|ledger|personnel)$/,
  /^api\/brief\/[\w-]+$/,
  /^api\/jobs\/[\w-]+\/events$/,
  /^incidents(\/[\w-]+(\/(close|responses))?)?$/,
  /^responses(\/[\w-]+)?$/,
  /^(resources|backlog|conflicts)$/,
  /^resources\/[\w-]+\/forecast$/,
  /^units\/[\w-]+\/release$/,
  /^healthz$/,
  // Demo dashboard (urban_response/ui_adapter.py)
  /^ui\/(health|incidents|responses|events|stations|hospitals|units|resources|scenario|reset)$/,
  /^ui\/incidents\/\d+\/(close|responses)$/,
  /^ui\/units\/[\w%.-]+\/release$/,
  /^ui\/resources\/[\w-]+\/forecast$/,
  /^ui\/world(\/(dispatch|advance|reassess))?$/,
];

const json = (status, detail) =>
  new Response(JSON.stringify({ detail }), { status, headers: { "content-type": "application/json" } });

async function handle(request) {
  const url = new URL(request.url);
  const path = (url.searchParams.get("path") || "").replace(/^\/+/, "");
  url.searchParams.delete("path");

  if (!BACKEND) return json(503, "SURU_BACKEND_URL is not configured on Vercel");
  if (!ALLOWED.some((re) => re.test(path))) return json(404, `Not proxied: /${path}`);

  const headers = new Headers({ accept: request.headers.get("accept") || "*/*" });
  const ct = request.headers.get("content-type");
  if (ct) headers.set("content-type", ct);
  if (TOKEN) headers.set("authorization", `Bearer ${TOKEN}`);

  const hasBody = !["GET", "HEAD"].includes(request.method);
  let upstream;
  try {
    upstream = await fetch(`${BACKEND}/${path}${url.search}`, {
      method: request.method,
      headers,
      body: hasBody ? await request.text() : undefined,
      signal: request.signal, // stop the upstream SSE when the operator closes the tab
      cache: "no-store",
    });
  } catch (e) {
    return json(502, `DGX backend unreachable (${e.cause?.code || e.message})`);
  }

  // Tunnel error pages (Cloudflare 502/530, Tailscale 502) are HTML; turn them into a clear JSON error.
  const upstreamType = upstream.headers.get("content-type") || "";
  if (!upstream.ok && !upstreamType.includes("json")) {
    return json(502, `DGX backend unreachable (tunnel returned ${upstream.status})`);
  }

  const out = new Headers({ "cache-control": "no-store" });
  if (upstreamType) out.set("content-type", upstreamType);
  if (upstreamType.includes("text/event-stream")) {
    out.set("x-accel-buffering", "no");
    out.set("connection", "keep-alive");
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export const GET = handle;
export const POST = handle;
