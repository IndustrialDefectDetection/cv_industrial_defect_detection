import "server-only";
import { SlidingWindowQuota } from "@/lib/request-security";

const CHAT_REQUESTS_PER_HOUR = 6;
const ONE_HOUR_IN_MILLISECONDS = 60 * 60 * 1_000;

type ChatQuotaGlobal = typeof globalThis & {
  __mesChatQuota?: SlidingWindowQuota;
};

const chatQuotaGlobal = globalThis as ChatQuotaGlobal;

export const chatQuota = chatQuotaGlobal.__mesChatQuota
  ?? new SlidingWindowQuota(
    CHAT_REQUESTS_PER_HOUR,
    ONE_HOUR_IN_MILLISECONDS,
  );

if (process.env.NODE_ENV !== "production") {
  chatQuotaGlobal.__mesChatQuota = chatQuota;
}
