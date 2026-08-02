import { createHash, timingSafeEqual } from "node:crypto";
import { isIP } from "node:net";
import { readBoundedBody } from "./request-security.ts";

type RouteHandler = (request: Request) => Promise<Response>;

export const TRUSTED_AUTH_CLIENT_IP_HEADER = "x-mes-client-ip";
const TRUSTED_AUTH_PROXY_SECRET_HEADER = "x-mes-proxy-secret";
const UNTRUSTED_FORWARDING_HEADERS = [
  "cf-connecting-ip",
  "forwarded",
  "true-client-ip",
  "x-forwarded-for",
  "x-real-ip",
];

// A Request's headers are immutable, so replacing them means building a new
// Request. The obvious `new Request(request, { headers })` does not work here:
// Next.js hands a route its own Request subclass, and that copy constructor
// reaches for a private `#state` field only undici's own class declares. It
// threw "Cannot read private member #state from an object whose class did not
// declare it" on every single authentication call - which surfaced in the
// browser as a bare "Authentication failed", indistinguishable from a wrong
// password. Rebuilding from the URL stays on the public surface instead.
function withReplacedHeaders(request: Request, headers: Headers): Request {
  const carriesBody = request.method !== "GET" && request.method !== "HEAD";

  return new Request(request.url, {
    body: carriesBody ? request.body : undefined,
    headers,
    method: request.method,
    redirect: request.redirect,
    signal: request.signal,
    // Streaming a body through requires this, and it is absent from the DOM
    // typings even though the runtime demands it.
    ...(carriesBody ? { duplex: "half" } : {}),
  } as RequestInit);
}

function secretsMatch(expected: string, supplied: string): boolean {
  const expectedDigest = createHash("sha256").update(expected).digest();
  const suppliedDigest = createHash("sha256").update(supplied).digest();
  return timingSafeEqual(expectedDigest, suppliedDigest);
}

function ingressProblemResponse(): Response {
  return Response.json(
    { error: "Request did not arrive through the trusted ingress" },
    {
      status: 403,
      headers: { "Cache-Control": "no-store" },
    },
  );
}

export function withTrustedAuthIngress(
  handler: RouteHandler,
  trustedProxySecret: string | null,
): RouteHandler {
  return async (request: Request): Promise<Response> => {
    const headers = new Headers(request.headers);
    let clientIp = "127.0.0.1";

    if (trustedProxySecret !== null) {
      const suppliedSecret = headers.get(TRUSTED_AUTH_PROXY_SECRET_HEADER);
      const suppliedClientIp = headers
        .get(TRUSTED_AUTH_CLIENT_IP_HEADER)
        ?.trim();

      if (
        !suppliedSecret
        || !secretsMatch(trustedProxySecret, suppliedSecret)
        || !suppliedClientIp
        || suppliedClientIp.includes(",")
        || isIP(suppliedClientIp) === 0
      ) {
        return ingressProblemResponse();
      }

      clientIp = suppliedClientIp;
    }

    for (const name of UNTRUSTED_FORWARDING_HEADERS) {
      headers.delete(name);
    }
    headers.delete(TRUSTED_AUTH_CLIENT_IP_HEADER);
    headers.delete(TRUSTED_AUTH_PROXY_SECRET_HEADER);
    headers.set(TRUSTED_AUTH_CLIENT_IP_HEADER, clientIp);

    return handler(withReplacedHeaders(request, headers));
  };
}

export function withBoundedRequestBody(
  handler: RouteHandler,
  maximumBytes: number,
  maximumReadMilliseconds = 5_000,
): RouteHandler {
  return async (request: Request): Promise<Response> => {
    const contentEncoding = request.headers.get("content-encoding");
    if (contentEncoding && contentEncoding.toLowerCase() !== "identity") {
      return Response.json(
        { error: "Compressed request bodies are not accepted" },
        {
          status: 415,
          headers: { "Cache-Control": "no-store" },
        },
      );
    }

    const bodyResult = await readBoundedBody(
      request,
      maximumBytes,
      maximumReadMilliseconds,
    );
    if (!bodyResult.ok) {
      return Response.json(
        { error: bodyResult.problem.error },
        {
          status: bodyResult.problem.status,
          headers: { "Cache-Control": "no-store" },
        },
      );
    }

    const headers = new Headers(request.headers);
    headers.set("Content-Length", String(bodyResult.value.byteLength));
    headers.delete("Transfer-Encoding");

    return handler(
      new Request(request.url, {
        body: bodyResult.value.byteLength > 0
          ? Uint8Array.from(bodyResult.value).buffer
          : undefined,
        headers,
        method: request.method,
        signal: request.signal,
      }),
    );
  };
}
