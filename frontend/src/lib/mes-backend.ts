import "server-only";
import {
  isValidMesUserId,
  readMesBackendRuntimeConfig,
} from "@/lib/security-config";

const config = readMesBackendRuntimeConfig(process.env);

const CLIENT_BACKEND_STATUSES = new Set([400, 409, 429, 503]);

const BACKEND_ERROR_MESSAGES: Record<number, string> = {
  400: "The assistant request was invalid",
  409: "Another analysis is already running",
  429: "The analysis limit has been reached",
  503: "The assistant is temporarily unavailable",
};

function backendUrl(path: string): URL {
  if (!path.startsWith("/") || path.startsWith("//")) {
    throw new Error("MES backend paths must be same-origin absolute paths");
  }

  return new URL(path, `${config.baseURL}/`);
}

export function fetchMesBackend(
  path: string,
  authenticatedUserId: string,
  init: RequestInit = {},
): Promise<Response> {
  if (!isValidMesUserId(authenticatedUserId)) {
    throw new Error("Authenticated user ID is invalid");
  }

  const headers = new Headers(init.headers);
  headers.set("X-MES-Internal-Token", config.internalApiToken);
  headers.set("X-MES-User-ID", authenticatedUserId);

  return fetch(backendUrl(path), {
    ...init,
    headers,
    redirect: "error",
  });
}

export function isBackendContentType(
  response: Response,
  expectedType: string,
): boolean {
  return response.headers
    .get("content-type")
    ?.split(";", 1)[0]
    .trim()
    .toLowerCase() === expectedType;
}

export async function sanitizedBackendErrorResponse(
  response: Response,
): Promise<Response> {
  await response.body?.cancel().catch(() => undefined);

  const status = CLIENT_BACKEND_STATUSES.has(response.status)
    ? response.status
    : 502;
  const headers = new Headers({
    "Cache-Control": "no-store",
  });

  if (status === 429) {
    const retryAfter = response.headers.get("retry-after");
    if (retryAfter && /^\d+$/.test(retryAfter)) {
      headers.set("Retry-After", retryAfter);
    }
  }

  return Response.json(
    {
      error: BACKEND_ERROR_MESSAGES[status] ?? "Assistant backend request failed",
    },
    {
      status,
      headers,
    },
  );
}
