export type ChatSidebarDisabledState = {
  newChat: boolean;
  selectChat: boolean;
  pinChat: boolean;
  deleteChat: boolean;
};

export type ChatComposerControlState = {
  action: "send" | "cancel";
  disabled: boolean;
};

export type CancelAcknowledgementState =
  | "accepted"
  | "retry_on_start"
  | "rejected";

type ChatSidebarState = {
  isHistoryLoading: boolean;
  isStartingTurn?: boolean;
  isAnalysisRunning: boolean;
  deletingChatId: string | null;
  pinningChatId: string | null;
};

type ChatComposerState = ChatSidebarState & {
  isViewingRunningChat: boolean;
  isCancelling: boolean;
};

export function getChatSidebarDisabledState({
  isHistoryLoading,
  isStartingTurn = false,
  isAnalysisRunning,
  deletingChatId,
  pinningChatId,
}: ChatSidebarState): ChatSidebarDisabledState {
  const isMutationBusy = isHistoryLoading
    || isStartingTurn
    || deletingChatId !== null
    || pinningChatId !== null;

  return {
    newChat: isMutationBusy || isAnalysisRunning,
    selectChat: isMutationBusy,
    pinChat: isMutationBusy,
    deleteChat: isMutationBusy || isAnalysisRunning,
  };
}

export function getChatComposerControlState({
  isHistoryLoading,
  isAnalysisRunning,
  isViewingRunningChat,
  isCancelling,
  deletingChatId,
  pinningChatId,
}: ChatComposerState): ChatComposerControlState {
  if (isAnalysisRunning && isViewingRunningChat) {
    return {
      action: "cancel",
      disabled: isCancelling,
    };
  }

  return {
    action: "send",
    disabled: isAnalysisRunning
      || isHistoryLoading
      || deletingChatId !== null
      || pinningChatId !== null,
  };
}

export function shouldApplyAnalysisToActiveChat(
  activeChatId: string | null,
  sourceChatId: string | null,
) {
  return activeChatId === sourceChatId;
}

export function getCancelAcknowledgementState(
  cancelling: boolean,
  chatStarted: boolean,
): CancelAcknowledgementState {
  if (cancelling) {
    return "accepted";
  }
  return chatStarted ? "rejected" : "retry_on_start";
}

export function buildChatTurnPayload(
  conversationId: string,
  messageId: string,
  userInput: string,
) {
  return {
    conversationId,
    messageId,
    user_input: userInput,
  };
}

type PersistedChatTurnOptions<ResponseType> = {
  savePending: () => Promise<{ id: string } | null>;
  onPersisted: (conversationId: string) => void | Promise<void>;
  isCancelled: () => boolean;
  requestAnalysis: (conversationId: string) => Promise<ResponseType>;
};

export type PersistedChatTurnResult<ResponseType> =
  | { status: "save_failed" }
  | { status: "cancelled"; conversationId: string }
  | {
    status: "response";
    conversationId: string;
    response: ResponseType;
  };

export async function runPersistedChatTurn<ResponseType>({
  savePending,
  onPersisted,
  isCancelled,
  requestAnalysis,
}: PersistedChatTurnOptions<ResponseType>): Promise<
  PersistedChatTurnResult<ResponseType>
> {
  const savedChat = await savePending();
  if (!savedChat) {
    return { status: "save_failed" };
  }

  await onPersisted(savedChat.id);
  if (isCancelled()) {
    return {
      status: "cancelled",
      conversationId: savedChat.id,
    };
  }

  return {
    status: "response",
    conversationId: savedChat.id,
    response: await requestAnalysis(savedChat.id),
  };
}

export function mergeSavedChatPreservingPin<
  ChatType extends { id: string; isPinned: boolean },
>(
  currentChats: readonly ChatType[],
  savedChat: ChatType,
) {
  const currentChat = currentChats.find((chat) => chat.id === savedChat.id);
  const mergedChat = currentChat
    ? { ...savedChat, isPinned: currentChat.isPinned }
    : savedChat;

  return [
    mergedChat,
    ...currentChats.filter((chat) => chat.id !== savedChat.id),
  ];
}
