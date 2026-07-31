import { auth } from "@/lib/auth";

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

export async function GET(request: Request) {
  const session = await auth.api.getSession({
    headers: request.headers,
  });

  if (!session) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const backendUrl = process.env.BACKEND_URL;
  if (!backendUrl) {
    return Response.json(
      { error: "Backend URL is not configured" },
      { status: 500 }
    );
  }

  const requestUrl = new URL(request.url);
  const requestedSince = Number(requestUrl.searchParams.get("since") ?? "0");
  const since = Number.isSafeInteger(requestedSince) && requestedSince >= 0
    ? requestedSince
    : 0;

  const response = await fetch(`${backendUrl}/trace?since=${since}`, {
    cache: "no-store",
  });
  const data = await response.json();

  if (!response.ok) {
    return Response.json(data, { status: response.status });
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

  return Response.json({
    seq: typeof data.seq === "number" ? data.seq : 0,
    run: {
      id: typeof data.run?.run_id === "string" ? data.run.run_id : null,
      status: typeof data.run?.status === "string"
        ? data.run.status
        : "idle",
    },
    current: {
      agent: typeof data.current?.agent === "string"
        ? data.current.agent
        : null,
      tool: typeof data.current?.tool === "string"
        ? data.current.tool
        : null,
    },
    events,
  });
}
