import { auth } from "@/lib/auth";
import {
  fetchMesBackend,
  isBackendContentType,
  sanitizedBackendErrorResponse,
} from "@/lib/mes-backend";

type TraceEvent = {
  seq?: unknown;
  agent?: unknown;
  kind?: unknown;
  tool_name?: unknown;
};

const visibleEventKinds = new Set([
  "run_start",
  "run_end",
  "agent_start",
  "agent_end",
  "tool_start",
  "tool_end",
]);
const BACKEND_READ_TIMEOUT_MILLISECONDS = 10_000;

export async function GET(request: Request) {
  const session = await auth.api.getSession({
    headers: request.headers,
  });

  if (!session) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const requestUrl = new URL(request.url);
  const requestedSince = Number(requestUrl.searchParams.get("since") ?? "0");
  const since = Number.isSafeInteger(requestedSince) && requestedSince >= 0
    ? requestedSince
    : 0;

  let response: Response;

  try {
    response = await fetchMesBackend(`/trace?since=${since}`, session.user.id, {
      cache: "no-store",
      signal: AbortSignal.timeout(BACKEND_READ_TIMEOUT_MILLISECONDS),
    });
  } catch {
    return Response.json(
      { error: "Assistant backend is unavailable" },
      {
        status: 502,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  if (!response.ok) {
    return sanitizedBackendErrorResponse(response);
  }

  if (!isBackendContentType(response, "application/json")) {
    await response.body?.cancel().catch(() => undefined);
    return Response.json(
      { error: "Backend response used an unexpected content type" },
      {
        status: 502,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  let data: Record<string, unknown>;
  try {
    const parsedData = await response.json();
    data = typeof parsedData === "object" && parsedData !== null
      ? parsedData as Record<string, unknown>
      : {};
  } catch {
    return Response.json(
      { error: "Backend response was not valid JSON" },
      {
        status: 502,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  const events = Array.isArray(data.events)
    ? data.events
      .filter((event: TraceEvent) =>
        typeof event.kind === "string"
        && visibleEventKinds.has(event.kind)
      )
      .map((event: TraceEvent) => ({
        seq: typeof event.seq === "number" ? event.seq : 0,
        agent: typeof event.agent === "string" ? event.agent : null,
        kind: event.kind,
        tool: typeof event.tool_name === "string" ? event.tool_name : null,
      }))
    : [];

  const run = typeof data.run === "object" && data.run !== null
    ? data.run as Record<string, unknown>
    : {};
  const current = typeof data.current === "object" && data.current !== null
    ? data.current as Record<string, unknown>
    : {};

  return Response.json(
    {
      seq: typeof data.seq === "number" ? data.seq : 0,
      run: {
        id: typeof run.run_id === "string" ? run.run_id : null,
        status: typeof run.status === "string"
          ? run.status
          : "idle",
      },
      current: {
        agent: typeof current.agent === "string"
          ? current.agent
          : null,
        tool: typeof current.tool === "string"
          ? current.tool
          : null,
      },
      events,
    },
    {
      headers: {
        "Cache-Control": "no-store",
      },
    },
  );
}
