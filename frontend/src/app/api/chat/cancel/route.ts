import { auth, trustedAppOrigin } from "@/lib/auth";
import {
  fetchMesBackend,
  isBackendContentType,
  sanitizedBackendErrorResponse,
} from "@/lib/mes-backend";
import {
  isRecord,
  requestProblemResponse,
  validateSameOrigin,
} from "@/lib/request-security";

const BACKEND_CANCEL_TIMEOUT_MILLISECONDS = 10_000;

export async function POST(request: Request) {
  const session = await auth.api.getSession({
    headers: request.headers,
  });

  if (!session) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const originProblem = validateSameOrigin(request, trustedAppOrigin);
  if (originProblem) {
    return requestProblemResponse(originProblem);
  }

  let response: Response;

  try {
    response = await fetchMesBackend("/cancel", session.user.id, {
      method: "POST",
      cache: "no-store",
      signal: AbortSignal.timeout(BACKEND_CANCEL_TIMEOUT_MILLISECONDS),
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

  let data: unknown;
  try {
    data = await response.json();
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

  if (!isRecord(data) || typeof data.cancelling !== "boolean") {
    return Response.json(
      { error: "Backend response had an unexpected shape" },
      {
        status: 502,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  return Response.json(
    { cancelling: data.cancelling },
    {
      headers: {
        "Cache-Control": "no-store",
      },
    },
  );
}
