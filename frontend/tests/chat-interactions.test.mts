import assert from "node:assert/strict";
import test from "node:test";
import {
  buildChatTurnPayload,
  getCancelAcknowledgementState,
  getChatComposerControlState,
  getChatSidebarDisabledState,
  mergeSavedChatPreservingPin,
  runPersistedChatTurn,
  shouldApplyAnalysisToActiveChat,
} from "../src/lib/chat-interactions.ts";


test("analysis keeps chat selection and pinning available", () => {
  assert.deepEqual(
    getChatSidebarDisabledState({
      isHistoryLoading: false,
      isAnalysisRunning: true,
      deletingChatId: null,
      pinningChatId: null,
    }),
    {
      newChat: true,
      selectChat: false,
      pinChat: false,
      deleteChat: true,
    },
  );
});

test("pending persistence briefly locks chat navigation", () => {
  assert.deepEqual(
    getChatSidebarDisabledState({
      isHistoryLoading: false,
      isStartingTurn: true,
      isAnalysisRunning: true,
      deletingChatId: null,
      pinningChatId: null,
    }),
    {
      newChat: true,
      selectChat: true,
      pinChat: true,
      deleteChat: true,
    },
  );
});

test("cancel belongs only to the chat that started the analysis", () => {
  const sharedState = {
    isHistoryLoading: false,
    isAnalysisRunning: true,
    isCancelling: false,
    deletingChatId: null,
    pinningChatId: null,
  };

  assert.deepEqual(
    getChatComposerControlState({
      ...sharedState,
      isViewingRunningChat: true,
    }),
    {
      action: "cancel",
      disabled: false,
    },
  );
  assert.deepEqual(
    getChatComposerControlState({
      ...sharedState,
      isViewingRunningChat: false,
    }),
    {
      action: "send",
      disabled: true,
    },
  );
});

test("an unaccepted cancel retries only before the run starts", () => {
  assert.equal(
    getCancelAcknowledgementState(true, false),
    "accepted",
  );
  assert.equal(
    getCancelAcknowledgementState(false, false),
    "retry_on_start",
  );
  assert.equal(
    getCancelAcknowledgementState(false, true),
    "rejected",
  );
});


test("a completed background analysis does not replace another active chat", () => {
  assert.equal(shouldApplyAnalysisToActiveChat("chat-b", "chat-a"), false);
  assert.equal(shouldApplyAnalysisToActiveChat("chat-a", "chat-a"), true);
  assert.equal(shouldApplyAnalysisToActiveChat(null, null), true);
});


test("a chat turn uses the persisted source conversation", () => {
  assert.deepEqual(
    buildChatTurnPayload(
      "550e8400-e29b-41d4-a716-446655440000",
      "67e55044-10b1-426f-9247-bb680e5fe0c8",
      "continue the old analysis",
    ),
    {
      conversationId: "550e8400-e29b-41d4-a716-446655440000",
      messageId: "67e55044-10b1-426f-9247-bb680e5fe0c8",
      user_input: "continue the old analysis",
    },
  );
});

test("analysis waits for the server-issued conversation ID", async () => {
  const calls: string[] = [];
  let resolveSave: (
    value: { id: string } | null,
  ) => void = () => undefined;
  const pendingSave = new Promise<{ id: string } | null>((resolve) => {
    resolveSave = resolve;
  });

  const turnPromise = runPersistedChatTurn({
    savePending: () => {
      calls.push("save pending");
      return pendingSave;
    },
    onPersisted: (conversationId) => {
      calls.push(`persisted ${conversationId}`);
    },
    isCancelled: () => false,
    requestAnalysis: async (conversationId) => {
      calls.push(`analyze ${conversationId}`);
      return "answer";
    },
  });

  await Promise.resolve();
  assert.deepEqual(calls, ["save pending"]);

  resolveSave({ id: "server-chat-a" });
  assert.deepEqual(await turnPromise, {
    status: "response",
    conversationId: "server-chat-a",
    response: "answer",
  });
  assert.deepEqual(calls, [
    "save pending",
    "persisted server-chat-a",
    "analyze server-chat-a",
  ]);
});

test("failed persistence and pre-analysis cancel never call the backend", async () => {
  let backendCalls = 0;

  const failedSave = await runPersistedChatTurn({
    savePending: async () => null,
    onPersisted: () => undefined,
    isCancelled: () => false,
    requestAnalysis: async () => {
      backendCalls += 1;
      return "unexpected";
    },
  });
  const cancelled = await runPersistedChatTurn({
    savePending: async () => ({ id: "server-chat-a" }),
    onPersisted: () => undefined,
    isCancelled: () => true,
    requestAnalysis: async () => {
      backendCalls += 1;
      return "unexpected";
    },
  });

  assert.deepEqual(failedSave, { status: "save_failed" });
  assert.deepEqual(cancelled, {
    status: "cancelled",
    conversationId: "server-chat-a",
  });
  assert.equal(backendCalls, 0);
});


test("a saved analysis keeps a concurrent local pin change", () => {
  const currentChats = [
    {
      id: "chat-a",
      isPinned: true,
      messages: ["question"],
    },
    {
      id: "chat-b",
      isPinned: false,
      messages: ["other chat"],
    },
  ];

  const mergedChats = mergeSavedChatPreservingPin(currentChats, {
    id: "chat-a",
    isPinned: false,
    messages: ["question", "answer"],
  });

  assert.deepEqual(mergedChats, [
    {
      id: "chat-a",
      isPinned: true,
      messages: ["question", "answer"],
    },
    currentChats[1],
  ]);
});
