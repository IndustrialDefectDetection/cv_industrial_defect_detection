import assert from "node:assert/strict";
import test from "node:test";
import {
  createPersistingChatStream,
  MAXIMUM_CHAT_STREAM_LINE_BYTES,
} from "../src/lib/chat-stream.ts";

const encoder = new TextEncoder();

function sourceFromChunks(chunks: Uint8Array[]) {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) {
        controller.enqueue(chunk);
      }
      controller.close();
    },
  });
}

async function readEvents(stream: ReadableStream<Uint8Array>) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let text = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      text += decoder.decode();
      break;
    }
    text += decoder.decode(value, { stream: true });
  }

  return text
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

test("results are persisted once before the server message ID is streamed", async () => {
  const upstream = [
    JSON.stringify({ type: "started" }),
    JSON.stringify({ type: "heartbeat" }),
    JSON.stringify({
      type: "result",
      data: {
        analysis: "Machine 🚲 maintenance correlation",
        messageId: "browser-must-not-control-this",
      },
    }),
    "",
  ].join("\n");
  const bytes = encoder.encode(upstream);
  const emojiOffset = encoder.encode(
    upstream.slice(0, upstream.indexOf("🚲")),
  ).byteLength;
  const persisted: string[] = [];

  const events = await readEvents(createPersistingChatStream(
    sourceFromChunks([
      bytes.slice(0, emojiOffset + 1),
      bytes.slice(emojiOffset + 1, emojiOffset + 3),
      bytes.slice(emojiOffset + 3),
    ]),
    async (analysis) => {
      persisted.push(analysis);
      return "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11";
    },
  ));

  assert.deepEqual(persisted, ["Machine 🚲 maintenance correlation"]);
  assert.deepEqual(events, [
    { type: "started" },
    { type: "heartbeat" },
    {
      type: "result",
      data: {
        analysis: "Machine 🚲 maintenance correlation",
        messageId: "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
      },
    },
  ]);
});

test("cancelled and failed analyses never persist assistant history", async () => {
  let persistenceCalls = 0;
  const persist = async () => {
    persistenceCalls += 1;
    return "unexpected";
  };

  const cancelled = await readEvents(createPersistingChatStream(
    sourceFromChunks([
      encoder.encode(
        `${JSON.stringify({
          type: "result",
          data: { status: "cancelled" },
        })}\n`,
      ),
    ]),
    persist,
  ));
  const failed = await readEvents(createPersistingChatStream(
    sourceFromChunks([
      encoder.encode(
        `${JSON.stringify({
          type: "error",
          error: "internal database detail",
        })}\n`,
      ),
    ]),
    persist,
  ));

  assert.equal(persistenceCalls, 0);
  assert.deepEqual(cancelled, [
    { type: "result", data: { status: "cancelled" } },
  ]);
  assert.deepEqual(failed, [
    { type: "error", error: "The analysis failed" },
  ]);
});

test("persistence failure becomes a terminal error without leaking a result", async () => {
  const events = await readEvents(createPersistingChatStream(
    sourceFromChunks([
      encoder.encode(
        `${JSON.stringify({
          type: "result",
          data: { analysis: "finished" },
        })}\n`,
      ),
    ]),
    async () => {
      throw new Error("database detail");
    },
  ));

  assert.deepEqual(events, [
    {
      type: "error",
      error: "The assistant response could not be saved",
    },
  ]);
});

test("invalid, unknown, and oversized stream events fail closed", async () => {
  const invalidInputs = [
    "{not-json}\n",
    `${JSON.stringify({ type: "unknown" })}\n`,
    `${"x".repeat(MAXIMUM_CHAT_STREAM_LINE_BYTES + 1)}\n`,
  ];

  for (const input of invalidInputs) {
    let persistenceCalls = 0;
    const events = await readEvents(createPersistingChatStream(
      sourceFromChunks([encoder.encode(input)]),
      async () => {
        persistenceCalls += 1;
        return "unexpected";
      },
    ));

    assert.equal(persistenceCalls, 0);
    assert.deepEqual(events, [
      {
        type: "error",
        error: "The assistant returned an invalid response",
      },
    ]);
  }
});

test("an EOF result is persisted and duplicate terminals are ignored", async () => {
  let persistenceCalls = 0;
  const result = JSON.stringify({
    type: "result",
    data: { analysis: "first" },
  });
  const duplicate = JSON.stringify({
    type: "result",
    data: { analysis: "second" },
  });

  const events = await readEvents(createPersistingChatStream(
    sourceFromChunks([
      encoder.encode(`${result}\n${duplicate}`),
    ]),
    async () => {
      persistenceCalls += 1;
      return "server-message";
    },
  ));

  assert.equal(persistenceCalls, 1);
  assert.deepEqual(events, [
    {
      type: "result",
      data: {
        analysis: "first",
        messageId: "server-message",
      },
    },
  ]);
});

test("downstream cancellation propagates to the backend stream", async () => {
  let cancelled = false;
  let markCancelled: () => void = () => undefined;
  const cancellationReachedSource = new Promise<void>((resolve) => {
    markCancelled = resolve;
  });
  const source = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode('{"type":"started"}\n'));
    },
    cancel() {
      cancelled = true;
      markCancelled();
    },
  });
  const reader = createPersistingChatStream(
    source,
    async () => "unexpected",
  ).getReader();

  const first = await reader.read();
  assert.equal(first.done, false);
  await reader.cancel("browser disconnected");
  await cancellationReachedSource;

  assert.equal(cancelled, true);
});
