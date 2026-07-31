import { auth, trustedAppOrigin } from "@/lib/auth";
import { chatQuota } from "@/lib/chat-quota";
import {
  fetchMesBackend,
  isBackendContentType,
  sanitizedBackendErrorResponse,
} from "@/lib/mes-backend";
import {
  hasExactKeys,
  isRecord,
  readBoundedJson,
  requestProblemResponse,
  validateJsonRequest,
  validateSameOrigin,
} from "@/lib/request-security";

const MAXIMUM_CHAT_REQUEST_BYTES = 8_192;
const MAXIMUM_USER_INPUT_CHARACTERS = 4_000;
const BACKEND_CHAT_TIMEOUT_MILLISECONDS = 10 * 60 * 1_000;

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

  const contentTypeProblem = validateJsonRequest(request);
  if (contentTypeProblem) {
    return requestProblemResponse(contentTypeProblem);
  }

  const parsedBody = await readBoundedJson(
    request,
    MAXIMUM_CHAT_REQUEST_BYTES,
  );
  if (!parsedBody.ok) {
    return requestProblemResponse(parsedBody.problem);
  }

  if (
    !isRecord(parsedBody.value)
    || !hasExactKeys(parsedBody.value, ["user_input"])
    || typeof parsedBody.value.user_input !== "string"
  ) {
    return Response.json(
      { error: "Request body must contain only a user_input string" },
      {
        status: 400,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  const userInput = parsedBody.value.user_input.trim();
  if (
    userInput.length === 0
    || userInput.length > MAXIMUM_USER_INPUT_CHARACTERS
  ) {
    return Response.json(
      {
        error: `user_input must be between 1 and ${MAXIMUM_USER_INPUT_CHARACTERS} characters`,
      },
      {
        status: 400,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  const quota = chatQuota.consume(session.user.id);
  if (!quota.allowed) {
    return Response.json(
      { error: "Hourly chat request limit reached" },
      {
        status: 429,
        headers: {
          "Cache-Control": "no-store",
          "Retry-After": String(quota.retryAfterSeconds),
        },
      },
    );
  }

  let response: Response;

  try {
    response = await fetchMesBackend("/chat/", session.user.id, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        user_input: userInput,
      }),
      cache: "no-store",
      signal: AbortSignal.timeout(BACKEND_CHAT_TIMEOUT_MILLISECONDS),
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

  if (!response.body) {
    return Response.json(
      { error: "Backend response did not include a stream" },
      {
        status: 502,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  if (!isBackendContentType(response, "application/x-ndjson")) {
    await response.body.cancel().catch(() => undefined);
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

  return new Response(response.body, {
    status: response.status,
    headers: {
      "Content-Type": "application/x-ndjson",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no",
    },
  });
}
