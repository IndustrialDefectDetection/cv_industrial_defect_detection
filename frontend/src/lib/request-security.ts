export type RequestProblem = {
  error: string;
  status: number;
};

export type JsonReadResult =
  | { ok: true; value: unknown }
  | { ok: false; problem: RequestProblem };

export type BodyReadResult =
  | { ok: true; value: Uint8Array }
  | { ok: false; problem: RequestProblem };

type QuotaEntry = {
  timestamps: number[];
};

const JSON_CONTENT_TYPE = "application/json";

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function hasExactKeys(
  value: Record<string, unknown>,
  expectedKeys: readonly string[],
): boolean {
  const actualKeys = Object.keys(value).sort();
  const sortedExpectedKeys = [...expectedKeys].sort();

  return actualKeys.length === sortedExpectedKeys.length
    && actualKeys.every((key, index) => key === sortedExpectedKeys[index]);
}

export function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
    .test(value);
}

export function utf8Length(value: string): number {
  return new TextEncoder().encode(value).byteLength;
}

export function validateSameOrigin(
  request: Request,
  expectedOrigin: string,
): RequestProblem | null {
  const origin = request.headers.get("origin");

  if (!origin) {
    return {
      error: "Request origin is required",
      status: 403,
    };
  }

  let parsedOrigin: URL;

  try {
    parsedOrigin = new URL(origin);
  } catch {
    return {
      error: "Invalid request origin",
      status: 403,
    };
  }

  if (
    parsedOrigin.username
    || parsedOrigin.password
    || parsedOrigin.pathname !== "/"
    || parsedOrigin.search
    || parsedOrigin.hash
    || parsedOrigin.origin !== expectedOrigin
  ) {
    return {
      error: "Cross-origin request rejected",
      status: 403,
    };
  }

  const fetchSite = request.headers.get("sec-fetch-site");
  if (fetchSite && !["same-origin", "none"].includes(fetchSite)) {
    return {
      error: "Cross-origin request rejected",
      status: 403,
    };
  }

  return null;
}

export function validateJsonRequest(request: Request): RequestProblem | null {
  const contentEncoding = request.headers.get("content-encoding");
  if (contentEncoding && contentEncoding.toLowerCase() !== "identity") {
    return {
      error: "Compressed request bodies are not accepted",
      status: 415,
    };
  }

  const contentType = request.headers
    .get("content-type")
    ?.split(";", 1)[0]
    .trim()
    .toLowerCase();

  if (contentType !== JSON_CONTENT_TYPE) {
    return {
      error: "Content-Type must be application/json",
      status: 415,
    };
  }

  return null;
}

export async function readBoundedBody(
  request: Request,
  maximumBytes: number,
  maximumReadMilliseconds = 5_000,
): Promise<BodyReadResult> {
  const declaredLength = request.headers.get("content-length");

  if (declaredLength) {
    const parsedLength = Number(declaredLength);
    if (
      !Number.isSafeInteger(parsedLength)
      || parsedLength < 0
      || parsedLength > maximumBytes
    ) {
      return {
        ok: false,
        problem: {
          error: "Request body is too large",
          status: 413,
        },
      };
    }
  }

  if (!request.body) {
    return { ok: true, value: new Uint8Array() };
  }

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let totalBytes = 0;
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    void reader.cancel("Request body read timed out").catch(() => undefined);
  }, maximumReadMilliseconds);

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (timedOut) {
        return {
          ok: false,
          problem: {
            error: "Request body read timed out",
            status: 408,
          },
        };
      }
      if (done) {
        break;
      }

      totalBytes += value.byteLength;
      if (totalBytes > maximumBytes) {
        await reader.cancel();
        return {
          ok: false,
          problem: {
            error: "Request body is too large",
            status: 413,
          },
        };
      }

      chunks.push(value);
    }
  } catch {
    if (timedOut) {
      return {
        ok: false,
        problem: {
          error: "Request body read timed out",
          status: 408,
        },
      };
    }
    return {
      ok: false,
      problem: {
        error: "Unable to read request body",
        status: 400,
      },
    };
  } finally {
    clearTimeout(timeout);
    reader.releaseLock();
  }

  const body = new Uint8Array(totalBytes);
  let offset = 0;

  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }

  return { ok: true, value: body };
}

export async function readBoundedJson(
  request: Request,
  maximumBytes: number,
): Promise<JsonReadResult> {
  if (!request.body) {
    return {
      ok: false,
      problem: {
        error: "Request body is required",
        status: 400,
      },
    };
  }

  const bodyResult = await readBoundedBody(request, maximumBytes);
  if (!bodyResult.ok) {
    return bodyResult;
  }

  let text: string;

  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(
      bodyResult.value,
    );
  } catch {
    return {
      ok: false,
      problem: {
        error: "Request body must be valid UTF-8",
        status: 400,
      },
    };
  }

  try {
    return {
      ok: true,
      value: JSON.parse(text) as unknown,
    };
  } catch {
    return {
      ok: false,
      problem: {
        error: "Invalid JSON request body",
        status: 400,
      },
    };
  }
}

export function requestProblemResponse(problem: RequestProblem): Response {
  return Response.json(
    { error: problem.error },
    {
      status: problem.status,
      headers: {
        "Cache-Control": "no-store",
      },
    },
  );
}

export class SlidingWindowQuota {
  private readonly entries = new Map<string, QuotaEntry>();
  private readonly maximumRequests: number;
  private readonly maximumSubjects: number;
  private readonly windowMilliseconds: number;
  private requestsSinceSweep = 0;

  constructor(
    maximumRequests: number,
    windowMilliseconds: number,
    maximumSubjects = 10_000,
  ) {
    if (
      !Number.isSafeInteger(maximumRequests)
      || maximumRequests < 1
      || !Number.isSafeInteger(windowMilliseconds)
      || windowMilliseconds < 1
      || !Number.isSafeInteger(maximumSubjects)
      || maximumSubjects < 1
    ) {
      throw new Error("Quota limits must be positive safe integers");
    }

    this.maximumRequests = maximumRequests;
    this.windowMilliseconds = windowMilliseconds;
    this.maximumSubjects = maximumSubjects;
  }

  consume(
    subject: string,
    now = Date.now(),
  ): { allowed: true } | { allowed: false; retryAfterSeconds: number } {
    this.requestsSinceSweep += 1;
    if (this.requestsSinceSweep >= 100) {
      this.sweep(now);
      this.requestsSinceSweep = 0;
    }

    const cutoff = now - this.windowMilliseconds;
    const timestamps = (this.entries.get(subject)?.timestamps ?? [])
      .filter((timestamp) => timestamp > cutoff);

    if (timestamps.length >= this.maximumRequests) {
      const retryAfterMilliseconds = this.windowMilliseconds
        - (now - timestamps[0]);

      this.entries.set(subject, { timestamps });
      return {
        allowed: false,
        retryAfterSeconds: Math.max(
          1,
          Math.ceil(retryAfterMilliseconds / 1_000),
        ),
      };
    }

    timestamps.push(now);
    this.entries.set(subject, { timestamps });
    this.enforceSubjectLimit();

    return { allowed: true };
  }

  private sweep(now: number): void {
    const cutoff = now - this.windowMilliseconds;

    for (const [subject, entry] of this.entries) {
      const timestamps = entry.timestamps.filter(
        (timestamp) => timestamp > cutoff,
      );

      if (timestamps.length === 0) {
        this.entries.delete(subject);
      } else {
        this.entries.set(subject, { timestamps });
      }
    }
  }

  private enforceSubjectLimit(): void {
    while (this.entries.size > this.maximumSubjects) {
      const oldestSubject = this.entries.keys().next().value;
      if (typeof oldestSubject !== "string") {
        break;
      }
      this.entries.delete(oldestSubject);
    }
  }
}
