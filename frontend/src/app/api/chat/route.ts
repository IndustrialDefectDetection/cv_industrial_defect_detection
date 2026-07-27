export async function POST(request: Request) {
  const body = await request.json();
  const backendUrl = process.env.BACKEND_URL;

  const response = await fetch(`${backendUrl}/chat/`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });

  const data = await response.json();

  return Response.json(data, {
    status: response.status,
  });
}