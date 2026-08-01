import {
  hasExactKeys,
  isRecord,
  isUuid,
  utf8Length,
} from "./request-security.ts";
import { MAXIMUM_USER_INPUT_CHARACTERS } from "./chat-history.ts";

export const MAXIMUM_CHAT_CONTEXT_MESSAGES = 20;
export const MAXIMUM_CHAT_CONTEXT_BYTES = 64 * 1024;
export { MAXIMUM_USER_INPUT_CHARACTERS };

export type ChatContextMessage = {
  role: "user" | "assistant";
  content: string;
};

export type ChatTurnBody = {
  conversationId: string;
  messageId: string;
  userInput: string;
};

type StoredContextRow = ChatContextMessage & {
  id: string;
  position: number;
};

type ChatContextQuery = (
  sql: string,
  parameters: unknown[],
) => Promise<{ rows: StoredContextRow[] }>;

export type OwnedChatContextResult =
  | {
    status: "ok";
    history: ChatContextMessage[];
  }
  | {
    status: "not_found";
  }
  | {
    status: "conflict";
  };

export function buildBackendChatRequest(
  conversationId: string,
  userInput: string,
  history: ChatContextMessage[],
) {
  return {
    conversation_id: conversationId,
    user_input: userInput,
    history,
  };
}

export function parseChatTurnBody(value: unknown): ChatTurnBody | null {
  if (
    !isRecord(value)
    || !hasExactKeys(
      value,
      ["conversationId", "messageId", "user_input"],
    )
    || typeof value.conversationId !== "string"
    || !isUuid(value.conversationId)
    || typeof value.messageId !== "string"
    || !isUuid(value.messageId)
    || typeof value.user_input !== "string"
  ) {
    return null;
  }

  const userInput = value.user_input.trim();
  if (
    userInput.length === 0
    || userInput.length > MAXIMUM_USER_INPUT_CHARACTERS
  ) {
    return null;
  }

  return {
    conversationId: value.conversationId,
    messageId: value.messageId,
    userInput,
  };
}

export async function loadOwnedChatContext(
  query: ChatContextQuery,
  userId: string,
  conversationId: string,
  currentMessageId: string,
  currentUserInput: string,
): Promise<OwnedChatContextResult> {
  const result = await query(
    `
      SELECT m.id, m.role, m.content, m.position
      FROM conversation AS c
      INNER JOIN message AS m ON m.conversation_id = c.id
      WHERE c.id = $1 AND c.user_id = $2
      ORDER BY m.position DESC
      LIMIT $3
    `,
    [
      conversationId,
      userId,
      MAXIMUM_CHAT_CONTEXT_MESSAGES + 1,
    ],
  );

  if (result.rows.length === 0) {
    return { status: "not_found" };
  }

  const [currentMessage, ...allPreviousMessagesNewestFirst] = result.rows;
  if (
    currentMessage.id !== currentMessageId
    || currentMessage.role !== "user"
    || currentMessage.content.trim() !== currentUserInput
  ) {
    return { status: "conflict" };
  }

  const previousMessagesNewestFirst = allPreviousMessagesNewestFirst.slice(
    0,
    MAXIMUM_CHAT_CONTEXT_MESSAGES,
  );
  for (const message of previousMessagesNewestFirst) {
    if (
      !["user", "assistant"].includes(message.role)
      || message.content.trim().length === 0
    ) {
      return { status: "conflict" };
    }
  }

  const completePairs: Array<[StoredContextRow, StoredContextRow]> = [];
  let pendingUserMessage: StoredContextRow | null = null;

  for (const message of [...previousMessagesNewestFirst].reverse()) {
    if (message.role === "user") {
      pendingUserMessage = message;
    } else if (pendingUserMessage) {
      completePairs.push([pendingUserMessage, message]);
      pendingUserMessage = null;
    }
  }

  const selectedPairsNewestFirst: Array<
    [StoredContextRow, StoredContextRow]
  > = [];
  let totalBytes = 0;

  for (let index = completePairs.length - 1; index >= 0; index -= 1) {
    const pair = completePairs[index];
    const pairBytes = utf8Length(pair[0].content)
      + utf8Length(pair[1].content);
    if (
      totalBytes + pairBytes > MAXIMUM_CHAT_CONTEXT_BYTES
      || (selectedPairsNewestFirst.length + 1) * 2
        > MAXIMUM_CHAT_CONTEXT_MESSAGES
    ) {
      break;
    }

    selectedPairsNewestFirst.push(pair);
    totalBytes += pairBytes;
  }

  const history = selectedPairsNewestFirst
    .reverse()
    .flat()
    .map(({ role, content }) => ({ role, content }));

  return {
    status: "ok",
    history,
  };
}
