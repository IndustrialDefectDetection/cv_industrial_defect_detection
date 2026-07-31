import { auth } from "@/lib/auth";

export async function POST(request: Request) {
  const session = await auth.api.getSession({
    headers: request.headers,
  });

  if (!session) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const body = await request.json();
  const backendUrl = process.env.BACKEND_URL;

  const response = await fetch(`${backendUrl}/chat/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    const data = await response.json();

    return Response.json(data, {
      status: response.status,
    });
  }

  if (!response.body) {
    return Response.json(
      { error: "Backend response did not include a stream" },
      { status: 502 }
    );
  }

  return new Response(response.body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("Content-Type")
        ?? "application/x-ndjson",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no",
    },
  });
}
