import assert from "node:assert/strict";
import test from "node:test";
import {
  MAXIMUM_MESSAGES_PER_CHAT,
  MAXIMUM_STORED_MESSAGE_BYTES,
  MAXIMUM_TOTAL_MESSAGE_BYTES,
  MAXIMUM_USER_INPUT_CHARACTERS,
  canAppendStoredMessage,
  parseAppendUserMessageBody,
} from "../src/lib/chat-history.ts";

const conversationId = "550e8400-e29b-41d4-a716-446655440000";
const userMessage = {
  id: "67e55044-10b1-426f-9247-bb680e5fe0c8",
  role: "user" as const,
  text: "maintenance correlation",
};

test("chat saves append one exact user message", () => {
  assert.deepEqual(
    parseAppendUserMessageBody({
      conversationId,
      message: userMessage,
    }),
    {
      conversationId,
      message: userMessage,
    },
  );
  assert.deepEqual(
    parseAppendUserMessageBody({ message: userMessage }),
    {
      conversationId: undefined,
      message: userMessage,
    },
  );
});

test("the browser cannot save assistant or bulk transcript messages", () => {
  for (const invalidBody of [
    {
      conversationId,
      message: { ...userMessage, role: "assistant" },
    },
    {
      conversationId,
      messages: [userMessage],
    },
    {
      conversationId,
      message: userMessage,
      unexpected: true,
    },
  ]) {
    assert.equal(parseAppendUserMessageBody(invalidBody), null);
  }
});

test("user message identity and text are strictly bounded", () => {
  const fourThousandUtf16Units = "🚲".repeat(
    MAXIMUM_USER_INPUT_CHARACTERS / 2,
  );

  assert.notEqual(
    parseAppendUserMessageBody({
      message: {
        ...userMessage,
        text: fourThousandUtf16Units,
      },
    }),
    null,
  );

  for (const invalidBody of [
    { conversationId: "not-a-uuid", message: userMessage },
    {
      conversationId,
      message: { ...userMessage, id: "not-a-uuid" },
    },
    {
      conversationId,
      message: { ...userMessage, text: "   " },
    },
    {
      conversationId,
      message: {
        ...userMessage,
        text: "x".repeat(MAXIMUM_USER_INPUT_CHARACTERS + 1),
      },
    },
    {
      conversationId,
      message: {
        ...userMessage,
        text: `${fourThousandUtf16Units}x`,
      },
    },
  ]) {
    assert.equal(parseAppendUserMessageBody(invalidBody), null);
  }
});

test("stored transcripts reserve the assistant slot and bound UTF-8 bytes", () => {
  assert.equal(
    canAppendStoredMessage(MAXIMUM_MESSAGES_PER_CHAT - 2, 0, "user", 1),
    true,
  );
  assert.equal(
    canAppendStoredMessage(MAXIMUM_MESSAGES_PER_CHAT - 1, 0, "user", 1),
    false,
  );
  assert.equal(
    canAppendStoredMessage(MAXIMUM_MESSAGES_PER_CHAT - 1, 0, "assistant"),
    true,
  );
  assert.equal(
    canAppendStoredMessage(MAXIMUM_MESSAGES_PER_CHAT, 0, "assistant"),
    false,
  );
  assert.equal(
    canAppendStoredMessage(0, MAXIMUM_TOTAL_MESSAGE_BYTES - 4, "🚲"),
    true,
  );
  assert.equal(
    canAppendStoredMessage(0, MAXIMUM_TOTAL_MESSAGE_BYTES - 3, "🚲"),
    false,
  );
  assert.equal(
    canAppendStoredMessage(
      0,
      MAXIMUM_TOTAL_MESSAGE_BYTES - MAXIMUM_STORED_MESSAGE_BYTES - 1,
      "x",
      1,
      MAXIMUM_STORED_MESSAGE_BYTES,
    ),
    true,
  );
  assert.equal(
    canAppendStoredMessage(
      0,
      MAXIMUM_TOTAL_MESSAGE_BYTES - MAXIMUM_STORED_MESSAGE_BYTES,
      "x",
      1,
      MAXIMUM_STORED_MESSAGE_BYTES,
    ),
    false,
  );
});
