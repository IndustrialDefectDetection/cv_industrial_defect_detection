import "server-only";
import { auth, trustedAppOrigin } from "@/lib/auth";
import { readBridgeRuntimeConfig } from "@/lib/security-config";
import { requestProblemResponse, validateSameOrigin } from "@/lib/request-security";

// Fires the demo camera burst on behalf of a signed-in user.
//
// This is a simulation control, not an operator feature: a real camera fires
// because steel moved under it. It exists so the chat can show what the vision
// half of the pipeline actually does without sending anyone to a terminal.
//
// The route adds nothing to the burst itself - the bridge decides what images
// are replayed and refuses a second concurrent run - so the only jobs here are
// proving the caller is signed in, proving the request came from this app, and
// not leaking the internal token or the bridge's error text to the browser.
const bridge = readBridgeRuntimeConfig(process.env);

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  const headers = new Headers(init.headers);
  headers.set("Cache-Control", "no-store");

  return Response.json(body, { ...init, headers });
}

export async function POST(request: Request) {
  const session = await auth.api.getSession({ headers: request.headers });
  if (!session) {
    return jsonResponse({ error: "Unauthorized" }, { status: 401 });
  }

  // No body to validate, so same-origin is the whole CSRF story here.
  const originProblem = validateSameOrigin(request, trustedAppOrigin);
  if (originProblem) {
    return requestProblemResponse(originProblem);
  }

  let response: Response;
  try {
    response = await fetch(new URL("/simulate", `${bridge.baseURL}/`), {
      method: "POST",
      headers: { "X-MES-Internal-Token": bridge.internalApiToken },
      redirect: "error",
      signal: AbortSignal.timeout(180_000),
    });
  } catch {
    return jsonResponse(
      { error: "The camera bridge is not running" },
      { status: 503 },
    );
  }

  if (response.status === 429) {
    const retryAfter = response.headers.get("retry-after");
    const headers = new Headers();
    if (retryAfter && /^\d+$/.test(retryAfter)) {
      headers.set("Retry-After", retryAfter);
    }
    await response.body?.cancel().catch(() => undefined);
    return jsonResponse(
      { error: "A burst is already running" },
      { status: 429, headers },
    );
  }

  if (!response.ok) {
    // The bridge's detail text can name server-side paths; report the shape of
    // the failure and keep the specifics in the bridge's own log.
    await response.body?.cancel().catch(() => undefined);
    return jsonResponse(
      { error: "The camera bridge could not run a burst" },
      { status: response.status === 503 ? 503 : 502 },
    );
  }

  const result = await response.json().catch(() => null);
  if (
    !result
    || typeof result.images_sent !== "number"
    || typeof result.saved_count !== "number"
    || typeof result.batched_count !== "number"
  ) {
    return jsonResponse(
      { error: "The camera bridge returned an unexpected response" },
      { status: 502 },
    );
  }

  return jsonResponse({
    imagesSent: result.images_sent,
    savedCount: result.saved_count,
    batchedCount: result.batched_count,
    machineName: typeof result.machine_name === "string" && result.machine_name
      ? result.machine_name
      : `machine ${result.machine_id}`,
  });
}
