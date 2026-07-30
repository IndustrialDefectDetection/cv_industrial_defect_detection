import { auth } from "@/lib/auth";

export async function POST(request: Request) {
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
      { status: 500 },
    );
  }

  const response = await fetch(`${backendUrl}/cancel`, {
    method: "POST",
  });
  const data = await response.json();

  return Response.json(data, {
    status: response.status,
  });
}
