import {
  hasExactKeys,
  isRecord,
  isUuid,
  utf8Length,
} from "./request-security.ts";

export const MAXIMUM_MESSAGES_PER_CHAT = 80;
export const MAXIMUM_STORED_MESSAGE_BYTES = 64_000;
export const MAXIMUM_TOTAL_MESSAGE_BYTES = 256_000;
export const MAXIMUM_USER_INPUT_CHARACTERS = 4_000;

export type StoredMessage = {
  id: string;
  text: string;
  role: "user" | "assistant";
};

export type AppendUserMessageBody = {
  conversationId?: string;
  message: StoredMessage & { role: "user" };
};

export function canAppendStoredMessage(
  messageCount: number,
  totalMessageBytes: number,
  nextMessage: string,
  reservedFollowingMessages = 0,
  reservedFollowingBytes = 0,
): boolean {
  return Number.isSafeInteger(messageCount)
    && messageCount >= 0
    && Number.isSafeInteger(totalMessageBytes)
    && totalMessageBytes >= 0
    && Number.isSafeInteger(reservedFollowingMessages)
    && reservedFollowingMessages >= 0
    && Number.isSafeInteger(reservedFollowingBytes)
    && reservedFollowingBytes >= 0
    && messageCount + 1 + reservedFollowingMessages
      <= MAXIMUM_MESSAGES_PER_CHAT
    && utf8Length(nextMessage) <= MAXIMUM_STORED_MESSAGE_BYTES
    && totalMessageBytes + utf8Length(nextMessage) + reservedFollowingBytes
      <= MAXIMUM_TOTAL_MESSAGE_BYTES;
}

function parseStoredMessage(value: unknown): StoredMessage | null {
  if (
    !isRecord(value)
    || !hasExactKeys(value, ["id", "role", "text"])
    || typeof value.id !== "string"
    || !isUuid(value.id)
    || typeof value.text !== "string"
    || value.text.trim().length === 0
    || utf8Length(value.text) > MAXIMUM_STORED_MESSAGE_BYTES
    || !["user", "assistant"].includes(
      typeof value.role === "string" ? value.role : "",
    )
  ) {
    return null;
  }

  return {
    id: value.id,
    text: value.text,
    role: value.role as StoredMessage["role"],
  };
}

export function parseAppendUserMessageBody(
  value: unknown,
): AppendUserMessageBody | null {
  if (!isRecord(value)) {
    return null;
  }

  const expectedKeys = value.conversationId === undefined
    ? ["message"]
    : ["conversationId", "message"];
  if (!hasExactKeys(value, expectedKeys)) {
    return null;
  }

  const conversationId = value.conversationId;
  if (
    conversationId !== undefined
    && (typeof conversationId !== "string" || !isUuid(conversationId))
  ) {
    return null;
  }

  const message = parseStoredMessage(value.message);
  if (
    !message
    || message.role !== "user"
    || message.text.trim().length > MAXIMUM_USER_INPUT_CHARACTERS
  ) {
    return null;
  }

  return {
    conversationId,
    message: {
      ...message,
      role: "user",
    },
  };
}
