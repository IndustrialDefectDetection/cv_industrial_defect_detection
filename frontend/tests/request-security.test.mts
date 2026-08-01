import assert from "node:assert/strict";
import test from "node:test";
import {
  hasExactKeys,
  isUuid,
  readBoundedJson,
  SlidingWindowQuota,
  validateJsonRequest,
  validateSameOrigin,
} from "../src/lib/request-security.ts";

function jsonRequest(
  body: string,
  headers: Record<string, string> = {},
): Request {
  return new Request("https://app.example.com/api/chat", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Origin: "https://app.example.com",
      ...headers,
    },
    body,
  });
}

test("same-origin validation rejects missing and foreign origins", () => {
  assert.equal(
    validateSameOrigin(
      jsonRequest("{}"),
      "https://app.example.com",
    ),
    null,
  );

  assert.equal(
    validateSameOrigin(
      jsonRequest("{}", { Origin: "https://evil.example" }),
      "https://app.example.com",
    )?.status,
    403,
  );
  assert.equal(
    validateSameOrigin(
      jsonRequest("{}", { Origin: "https://app.example.com/unexpected-path" }),
      "https://app.example.com",
    )?.status,
    403,
  );

  const missingOriginRequest = new Request(
    "https://app.example.com/api/chat",
    { method: "POST" },
  );
  assert.equal(
    validateSameOrigin(
      missingOriginRequest,
      "https://app.example.com",
    )?.status,
    403,
  );
});

test("JSON validation rejects other media types and compressed bodies", () => {
  assert.equal(validateJsonRequest(jsonRequest("{}")), null);
  assert.equal(
    validateJsonRequest(
      jsonRequest("{}", { "Content-Type": "text/plain" }),
    )?.status,
    415,
  );
  assert.equal(
    validateJsonRequest(
      jsonRequest("{}", { "Content-Encoding": "gzip" }),
    )?.status,
    415,
  );
});

test("bounded JSON parsing accepts valid input and rejects malformed input", async () => {
  assert.deepEqual(
    await readBoundedJson(jsonRequest('{"value":1}'), 64),
    {
      ok: true,
      value: { value: 1 },
    },
  );

  assert.equal(
    (await readBoundedJson(jsonRequest("{"), 64)).ok,
    false,
  );
  assert.equal(
    (await readBoundedJson(jsonRequest(`"${"x".repeat(100)}"`), 16)).ok,
    false,
  );
});

test("strict object and UUID checks reject ambiguous input", () => {
  assert.equal(
    hasExactKeys({ messages: [], unexpected: true }, ["messages"]),
    false,
  );
  assert.equal(isUuid("550e8400-e29b-41d4-a716-446655440000"), true);
  assert.equal(isUuid("../not-an-id"), false);
});

test("sliding-window quota blocks excess requests until the window expires", () => {
  const quota = new SlidingWindowQuota(2, 1_000);

  assert.deepEqual(quota.consume("user", 0), { allowed: true });
  assert.deepEqual(quota.consume("user", 100), { allowed: true });
  assert.deepEqual(
    quota.consume("user", 200),
    { allowed: false, retryAfterSeconds: 1 },
  );
  assert.deepEqual(quota.consume("user", 1_001), { allowed: true });
});
