import { MAXIMUM_STORED_MESSAGE_BYTES } from "./chat-history.ts";
import {
  isRecord,
  utf8Length,
} from "./request-security.ts";

export const MAXIMUM_CHAT_STREAM_LINE_BYTES =
  MAXIMUM_STORED_MESSAGE_BYTES * 6 + 4_096;

const encoder = new TextEncoder();

type PersistAssistant = (analysis: string) => Promise<string>;

function encodeEvent(event: unknown): Uint8Array {
  return encoder.encode(`${JSON.stringify(event)}\n`);
}

function terminalError(
  controller: TransformStreamDefaultController<Uint8Array>,
  error: string,
) {
  controller.enqueue(encodeEvent({ type: "error", error }));
  controller.terminate();
}

export function createPersistingChatStream(
  source: ReadableStream<Uint8Array>,
  persistAssistant: PersistAssistant,
): ReadableStream<Uint8Array> {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let buffer = "";
  let terminalSeen = false;

  async function processLine(
    rawLine: string,
    controller: TransformStreamDefaultController<Uint8Array>,
  ) {
    const line = rawLine.trim();
    if (!line || terminalSeen) {
      return;
    }

    if (utf8Length(line) > MAXIMUM_CHAT_STREAM_LINE_BYTES) {
      terminalSeen = true;
      terminalError(controller, "The assistant returned an invalid response");
      return;
    }

    let event: unknown;
    try {
      event = JSON.parse(line) as unknown;
    } catch {
      terminalSeen = true;
      terminalError(controller, "The assistant returned an invalid response");
      return;
    }

    if (!isRecord(event) || typeof event.type !== "string") {
      terminalSeen = true;
      terminalError(controller, "The assistant returned an invalid response");
      return;
    }

    if (event.type === "started" || event.type === "heartbeat") {
      controller.enqueue(encodeEvent({ type: event.type }));
      return;
    }

    if (event.type === "error") {
      terminalSeen = true;
      controller.enqueue(encodeEvent({
        type: "error",
        error: "The analysis failed",
      }));
      controller.terminate();
      return;
    }

    if (event.type !== "result" || !isRecord(event.data)) {
      terminalSeen = true;
      terminalError(controller, "The assistant returned an invalid response");
      return;
    }

    if (event.data.status === "cancelled") {
      terminalSeen = true;
      controller.enqueue(encodeEvent({
        type: "result",
        data: { status: "cancelled" },
      }));
      controller.terminate();
      return;
    }

    if (
      typeof event.data.analysis !== "string"
      || event.data.analysis.trim().length === 0
      || utf8Length(event.data.analysis) > MAXIMUM_STORED_MESSAGE_BYTES
    ) {
      terminalSeen = true;
      terminalError(controller, "The assistant returned an invalid response");
      return;
    }

    terminalSeen = true;
    try {
      const messageId = await persistAssistant(event.data.analysis);
      controller.enqueue(encodeEvent({
        type: "result",
        data: {
          analysis: event.data.analysis,
          messageId,
        },
      }));
      controller.terminate();
    } catch {
      terminalError(
        controller,
        "The assistant response could not be saved",
      );
    }
  }

  const transform = new TransformStream<Uint8Array, Uint8Array>({
    async transform(chunk, controller) {
      if (terminalSeen) {
        return;
      }

      try {
        buffer += decoder.decode(chunk, { stream: true });
      } catch {
        terminalSeen = true;
        terminalError(controller, "The assistant returned an invalid response");
        return;
      }

      let newlineIndex = buffer.indexOf("\n");
      while (newlineIndex !== -1 && !terminalSeen) {
        const line = buffer.slice(0, newlineIndex);
        buffer = buffer.slice(newlineIndex + 1);
        await processLine(line, controller);
        newlineIndex = buffer.indexOf("\n");
      }

      if (
        !terminalSeen
        && utf8Length(buffer) > MAXIMUM_CHAT_STREAM_LINE_BYTES
      ) {
        terminalSeen = true;
        terminalError(controller, "The assistant returned an invalid response");
      }
    },
    async flush(controller) {
      if (terminalSeen) {
        return;
      }

      try {
        buffer += decoder.decode();
      } catch {
        terminalSeen = true;
        terminalError(controller, "The assistant returned an invalid response");
        return;
      }

      if (buffer.trim()) {
        await processLine(buffer, controller);
      }

      if (!terminalSeen) {
        terminalSeen = true;
        terminalError(controller, "The assistant response ended unexpectedly");
      }
    },
  });

  return source.pipeThrough(transform);
}
