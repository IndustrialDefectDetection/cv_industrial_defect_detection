import { randomUUID } from "node:crypto";
import { auth, db, trustedAppOrigin } from "@/lib/auth";
import {
  buildBackendChatRequest,
  loadOwnedChatContext,
  parseChatTurnBody,
} from "@/lib/chat-context";
import {
  MAXIMUM_STORED_MESSAGE_BYTES,
  canAppendStoredMessage,
} from "@/lib/chat-history";
import { chatQuota } from "@/lib/chat-quota";
import { createPersistingChatStream } from "@/lib/chat-stream";
import {
  fetchMesBackend,
  isBackendContentType,
  sanitizedBackendErrorResponse,
} from "@/lib/mes-backend";
import {
  readBoundedJson,
  requestProblemResponse,
  utf8Length,
  validateJsonRequest,
  validateSameOrigin,
} from "@/lib/request-security";

const MAXIMUM_CHAT_REQUEST_BYTES = 32_000;
const BACKEND_CHAT_TIMEOUT_MILLISECONDS = 10 * 60 * 1_000;

async function persistAssistantMessage(
  userId: string,
  conversationId: string,
  currentMessageId: string,
  analysis: string,
): Promise<string> {
  if (
    analysis.trim().length === 0
    || utf8Length(analysis) > MAXIMUM_STORED_MESSAGE_BYTES
  ) {
    throw new Error("Assistant response exceeded the saved-message limit");
  }

  const client = await db.connect();
  const assistantMessageId = randomUUID();

  try {
    await client.query("BEGIN");
    const conversation = await client.query<{ id: string }>(
      `
        SELECT id
        FROM conversation
        WHERE id = $1 AND user_id = $2
        FOR UPDATE
      `,
      [conversationId, userId],
    );
    if (conversation.rowCount === 0) {
      throw new Error("Conversation is no longer available");
    }

    const latestMessage = await client.query<{
      id: string;
      role: "user" | "assistant";
      position: number;
    }>(
      `
        SELECT id, role, position
        FROM message
        WHERE conversation_id = $1
        ORDER BY position DESC
        LIMIT 1
      `,
      [conversationId],
    );
    const currentMessage = latestMessage.rows[0];
    if (
      !currentMessage
      || currentMessage.id !== currentMessageId
      || currentMessage.role !== "user"
    ) {
      throw new Error("Conversation changed before the response was saved");
    }

    const totals = await client.query<{
      message_count: number;
      total_bytes: number;
    }>(
      `
        SELECT
          COUNT(*)::integer AS message_count,
          COALESCE(SUM(octet_length(content)), 0)::integer AS total_bytes
        FROM message
        WHERE conversation_id = $1
      `,
      [conversationId],
    );
    if (!canAppendStoredMessage(
      totals.rows[0].message_count,
      totals.rows[0].total_bytes,
      analysis,
    )) {
      throw new Error("Conversation reached the saved-history limit");
    }

    await client.query(
      `
        INSERT INTO message (id, conversation_id, role, content, position)
        VALUES ($1, $2, 'assistant', $3, $4)
      `,
      [
        assistantMessageId,
        conversationId,
        analysis,
        currentMessage.position + 1,
      ],
    );
    await client.query(
      `
        UPDATE conversation
        SET updated_at = now()
        WHERE id = $1 AND user_id = $2
      `,
      [conversationId, userId],
    );
    await client.query("COMMIT");
    return assistantMessageId;
  } catch (error) {
    await client.query("ROLLBACK");
    console.error("Failed to persist assistant response:", error);
    throw error;
  } finally {
    client.release();
  }
}

export async function POST(request: Request) {
  const session = await auth.api.getSession({
    headers: request.headers,
  });

  if (!session) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const originProblem = validateSameOrigin(request, trustedAppOrigin);
  if (originProblem) {
    return requestProblemResponse(originProblem);
  }

  const contentTypeProblem = validateJsonRequest(request);
  if (contentTypeProblem) {
    return requestProblemResponse(contentTypeProblem);
  }

  const parsedBody = await readBoundedJson(
    request,
    MAXIMUM_CHAT_REQUEST_BYTES,
  );
  if (!parsedBody.ok) {
    return requestProblemResponse(parsedBody.problem);
  }

  const chatTurn = parseChatTurnBody(parsedBody.value);
  if (!chatTurn) {
    return Response.json(
      {
        error: "Request body must contain conversationId, messageId, and user_input",
      },
      {
        status: 400,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  let context: Awaited<ReturnType<typeof loadOwnedChatContext>>;
  try {
    context = await loadOwnedChatContext(
      (sql, parameters) => db.query(sql, parameters),
      session.user.id,
      chatTurn.conversationId,
      chatTurn.messageId,
      chatTurn.userInput,
    );
  } catch (error) {
    console.error("Failed to load chat context:", error);
    return Response.json(
      { error: "Unable to load chat context" },
      {
        status: 500,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  if (context.status === "not_found") {
    return Response.json(
      { error: "Chat not found" },
      {
        status: 404,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  if (context.status === "conflict") {
    return Response.json(
      { error: "Chat changed before the analysis started" },
      {
        status: 409,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  const quota = chatQuota.consume(session.user.id);
  if (!quota.allowed) {
    return Response.json(
      { error: "Hourly chat request limit reached" },
      {
        status: 429,
        headers: {
          "Cache-Control": "no-store",
          "Retry-After": String(quota.retryAfterSeconds),
        },
      },
    );
  }

  let response: Response;

  try {
    response = await fetchMesBackend("/chat/", session.user.id, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(buildBackendChatRequest(
        chatTurn.conversationId,
        chatTurn.userInput,
        context.history,
      )),
      cache: "no-store",
      signal: AbortSignal.any([
        request.signal,
        AbortSignal.timeout(BACKEND_CHAT_TIMEOUT_MILLISECONDS),
      ]),
    });
  } catch {
    return Response.json(
      { error: "Assistant backend is unavailable" },
      {
        status: 502,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  if (!response.ok) {
    return sanitizedBackendErrorResponse(response);
  }

  if (!response.body) {
    return Response.json(
      { error: "Backend response did not include a stream" },
      {
        status: 502,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  if (!isBackendContentType(response, "application/x-ndjson")) {
    await response.body.cancel().catch(() => undefined);
    return Response.json(
      { error: "Backend response used an unexpected content type" },
      {
        status: 502,
        headers: {
          "Cache-Control": "no-store",
        },
      },
    );
  }

  const persistedStream = createPersistingChatStream(
    response.body,
    (analysis) => persistAssistantMessage(
      session.user.id,
      chatTurn.conversationId,
      chatTurn.messageId,
      analysis,
    ),
  );

  return new Response(persistedStream, {
    status: response.status,
    headers: {
      "Content-Type": "application/x-ndjson",
      "Cache-Control": "no-cache, no-transform",
      "X-Accel-Buffering": "no",
    },
  });
}
