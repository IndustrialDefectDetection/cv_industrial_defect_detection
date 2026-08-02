import assert from "node:assert/strict";
import test from "node:test";
import {
  withBoundedRequestBody,
  withTrustedAuthIngress,
} from "../src/lib/bounded-route.ts";


test("bounded route rejects declared and streamed oversized bodies", async () => {
  let calls = 0;
  const handler = withBoundedRequestBody(async () => {
    calls += 1;
    return new Response("unexpected");
  }, 64);

  const declared = await handler(
    new Request("https://app.example.com/api/auth/sign-in/email", {
      body: "{}",
      headers: { "Content-Length": "65" },
      method: "POST",
    }),
  );
  assert.equal(declared.status, 413);

  const streamed = await handler(
    new Request("https://app.example.com/api/auth/sign-in/email", {
      body: "x".repeat(65),
      method: "POST",
    }),
  );
  assert.equal(streamed.status, 413);
  assert.equal(calls, 0);
});


test("bounded route forwards only the materialized bounded body", async () => {
  const handler = withBoundedRequestBody(async (request) => {
    assert.equal(request.headers.get("content-length"), "13");
    assert.equal(await request.text(), '{"email":"a"}');
    return new Response("ok");
  }, 64);

  const response = await handler(
    new Request("https://app.example.com/api/auth/sign-in/email", {
      body: '{"email":"a"}',
      headers: { "Content-Type": "application/json" },
      method: "POST",
    }),
  );

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "ok");
});


test("bounded route rejects compressed bodies before the auth handler", async () => {
  const handler = withBoundedRequestBody(async () => {
    throw new Error("compressed body reached the auth handler");
  }, 64);

  const response = await handler(
    new Request("https://app.example.com/api/auth/sign-in/email", {
      body: "compressed",
      headers: { "Content-Encoding": "gzip" },
      method: "POST",
    }),
  );

  assert.equal(response.status, 415);
});


test("bounded route times out a slow request body", async () => {
  const handler = withBoundedRequestBody(async () => {
    throw new Error("slow body reached the auth handler");
  }, 64, 10);
  const body = new ReadableStream<Uint8Array>({
    start() {
      // Deliberately never emit or close.
    },
  });

  const response = await handler(
    new Request("https://app.example.com/api/auth/sign-in/email", {
      body,
      duplex: "half",
      method: "POST",
    } as RequestInit & { duplex: "half" }),
  );

  assert.equal(response.status, 408);
});


// Every other test in this file builds a genuine undici Request, and that is
// why they all passed while production 500ed on every single sign-in. Next.js
// hands a route handler its own Request class, and the copy constructor
// `new Request(request, { headers })` reaches for a private `#state` field
// that only undici's class declares, throwing "Cannot read private member
// #state from an object whose class did not declare it". The browser reported
// a bare "Authentication failed", indistinguishable from a wrong password.
//
// This stands in for that foreign class: it offers the public Request surface
// the wrapper is allowed to use and nothing else, so any reimplementation that
// reaches for internals fails here instead of in front of a user.
function foreignRequest(
  url: string,
  init: { body?: string; headers?: Record<string, string>; method?: string },
): Request {
  return {
    body: init.body ?? null,
    headers: new Headers(init.headers ?? {}),
    method: init.method ?? "GET",
    redirect: "follow",
    signal: AbortSignal.timeout(30_000),
    url,
  } as unknown as Request;
}


test("auth ingress accepts a Request from another implementation", async () => {
  const handler = withTrustedAuthIngress(async (request) => {
    assert.equal(request.url, "http://localhost:3000/api/auth/sign-in/email");
    assert.equal(request.headers.get("x-keep-me"), "kept");
    assert.equal(request.headers.get("x-mes-client-ip"), "127.0.0.1");
    return new Response("ok");
  }, null);

  const response = await handler(
    foreignRequest("http://localhost:3000/api/auth/sign-in/email", {
      headers: { "X-Keep-Me": "kept" },
    }),
  );

  assert.equal(response.status, 200);
});


test("auth ingress carries a foreign POST body through unchanged", async () => {
  const handler = withTrustedAuthIngress(async (request) => {
    assert.deepEqual(await request.json(), { email: "someone@example.com" });
    return new Response("ok");
  }, null);

  const response = await handler(
    foreignRequest("http://localhost:3000/api/auth/sign-in/email", {
      body: JSON.stringify({ email: "someone@example.com" }),
      headers: { "Content-Type": "application/json" },
      method: "POST",
    }),
  );

  assert.equal(response.status, 200);
});


test("local auth ingress discards spoofed forwarding headers", async () => {
  const handler = withTrustedAuthIngress(async (request) => {
    assert.equal(request.headers.get("x-mes-client-ip"), "127.0.0.1");
    assert.equal(request.headers.get("x-forwarded-for"), null);
    assert.equal(request.headers.get("x-real-ip"), null);
    return new Response("ok");
  }, null);

  const response = await handler(
    new Request("http://localhost:3000/api/auth/sign-in/email", {
      headers: {
        "X-Forwarded-For": "203.0.113.1",
        "X-Mes-Client-Ip": "198.51.100.5",
        "X-Real-Ip": "192.0.2.1",
      },
    }),
  );

  assert.equal(response.status, 200);
});


test("remote auth ingress requires a secret and one verified client IP", async () => {
  const handler = withTrustedAuthIngress(async (request) => {
    assert.equal(request.headers.get("x-mes-client-ip"), "203.0.113.9");
    assert.equal(request.headers.get("x-mes-proxy-secret"), null);
    return new Response("ok");
  }, "p".repeat(32));

  const spoofed = await handler(
    new Request("https://app.example.com/api/auth/sign-in/email", {
      headers: {
        "X-Mes-Client-Ip": "203.0.113.9",
        "X-Mes-Proxy-Secret": "wrong",
      },
    }),
  );
  assert.equal(spoofed.status, 403);

  const chained = await handler(
    new Request("https://app.example.com/api/auth/sign-in/email", {
      headers: {
        "X-Mes-Client-Ip": "203.0.113.9, 127.0.0.1",
        "X-Mes-Proxy-Secret": "p".repeat(32),
      },
    }),
  );
  assert.equal(chained.status, 403);

  const trusted = await handler(
    new Request("https://app.example.com/api/auth/sign-in/email", {
      headers: {
        "X-Mes-Client-Ip": "203.0.113.9",
        "X-Mes-Proxy-Secret": "p".repeat(32),
      },
    }),
  );
  assert.equal(trusted.status, 200);
});
