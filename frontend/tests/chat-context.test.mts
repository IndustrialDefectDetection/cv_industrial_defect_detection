import assert from "node:assert/strict";
import test from "node:test";
import {
  buildBackendChatRequest,
  loadOwnedChatContext,
  MAXIMUM_CHAT_CONTEXT_BYTES,
  MAXIMUM_CHAT_CONTEXT_MESSAGES,
  parseChatTurnBody,
} from "../src/lib/chat-context.ts";

const conversationId = "550e8400-e29b-41d4-a716-446655440000";
const messageId = "67e55044-10b1-426f-9247-bb680e5fe0c8";

test("chat turn bodies require an exact saved conversation contract", () => {
  assert.deepEqual(
    parseChatTurnBody({
      conversationId,
      messageId,
      user_input: "  continue the maintenance analysis  ",
    }),
    {
      conversationId,
      messageId,
      userInput: "continue the maintenance analysis",
    },
  );

  for (const invalidBody of [
    { user_input: "missing conversation" },
    { conversationId: null, messageId, user_input: "not saved" },
    {
      conversationId: "not-a-uuid",
      messageId,
      user_input: "invalid conversation",
    },
    { conversationId, messageId: "not-a-uuid", user_input: "invalid message" },
    { conversationId, messageId, user_input: "   " },
    { conversationId, messageId, user_input: "valid", history: [] },
  ]) {
    assert.equal(parseChatTurnBody(invalidBody), null);
  }
});

test("the Next boundary forwards the normalized backend contract", () => {
  assert.deepEqual(
    buildBackendChatRequest(
      conversationId,
      "continue",
      [
        { role: "user", content: "maintenance correlation" },
        { role: "assistant", content: "Which machines?" },
      ],
    ),
    {
      conversation_id: conversationId,
      user_input: "continue",
      history: [
        { role: "user", content: "maintenance correlation" },
        { role: "assistant", content: "Which machines?" },
      ],
    },
  );
});

test("chat context is owner-scoped, chronological, and excludes this turn", async () => {
  let capturedSql = "";
  let capturedParameters: unknown[] = [];
  const rows = [
    {
      id: messageId,
      role: "user" as const,
      content: "random machines",
      position: 4,
    },
    {
      id: "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
      role: "assistant" as const,
      content: "Which machines should I analyze?",
      position: 3,
    },
    {
      id: "9f8cfe5f-65d4-4b18-8d61-f6c36c93fd79",
      role: "user" as const,
      content: "maintenance correlation",
      position: 2,
    },
    {
      id: "16fd2706-8baf-433b-82eb-8c7fada847da",
      role: "assistant" as const,
      content: "This leading assistant is outside the retained turn.",
      position: 1,
    },
  ];

  const context = await loadOwnedChatContext(
    async (sql, parameters) => {
      capturedSql = sql;
      capturedParameters = parameters;
      return { rows };
    },
    "authenticated-user",
    conversationId,
    messageId,
    "random machines",
  );

  assert.match(capturedSql, /c\.id = \$1 AND c\.user_id = \$2/);
  assert.deepEqual(capturedParameters, [
    conversationId,
    "authenticated-user",
    MAXIMUM_CHAT_CONTEXT_MESSAGES + 1,
  ]);
  assert.deepEqual(context, {
    status: "ok",
    history: [
      { role: "user", content: "maintenance correlation" },
      {
        role: "assistant",
        content: "Which machines should I analyze?",
      },
    ],
  });
});

test("missing and changed conversations fail closed", async () => {
  const missing = await loadOwnedChatContext(
    async () => ({ rows: [] }),
    "authenticated-user",
    conversationId,
    messageId,
    "question",
  );
  assert.deepEqual(missing, { status: "not_found" });

  const changed = await loadOwnedChatContext(
    async () => ({
      rows: [
        {
          id: "978d22e1-9b4d-4d4f-a376-1f6d4e368a0b",
          role: "user",
          content: "stale question",
          position: 2,
        },
        {
          id: "978d22e1-9b4d-4d4f-a376-1f6d4e368a0c",
          role: "assistant",
          content: "old answer",
          position: 1,
        },
      ],
    }),
    "authenticated-user",
    conversationId,
    messageId,
    "stale question",
  );
  assert.deepEqual(changed, { status: "conflict" });
});

test("hydrated context remains bounded by message count and UTF-8 bytes", async () => {
  const rows = [
    {
      id: messageId,
      role: "user" as const,
      content: "current",
      position: 100,
    },
    ...Array.from({ length: 30 }, (_, index) => ({
      id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
      role: (index % 2 === 0 ? "assistant" : "user") as
        | "user"
        | "assistant",
      content: `${index}-${"🚲".repeat(3_000)}`,
      position: 99 - index,
    })),
  ];

  const context = await loadOwnedChatContext(
    async () => ({ rows }),
    "authenticated-user",
    conversationId,
    messageId,
    "current",
  );

  assert.equal(context.status, "ok");
  if (context.status !== "ok") {
    return;
  }

  assert.ok(context.history.length <= MAXIMUM_CHAT_CONTEXT_MESSAGES);
  assert.ok(
    context.history.reduce(
      (total, message) => total + Buffer.byteLength(message.content),
      0,
    ) <= MAXIMUM_CHAT_CONTEXT_BYTES,
  );
  assert.equal(context.history[0]?.role, "user");
  assert.equal(context.history.at(-1)?.role, "assistant");
  assert.ok(
    context.history.every(
      (message, index) =>
        message.role === (index % 2 === 0 ? "user" : "assistant"),
    ),
  );
});
