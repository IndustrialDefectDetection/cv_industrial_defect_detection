import { auth, trustedAuthProxySecret } from "@/lib/auth";
import {
  withBoundedRequestBody,
  withTrustedAuthIngress,
} from "@/lib/bounded-route";
import { toNextJsHandler } from "better-auth/next-js";

const handlers = toNextJsHandler(auth);

export const GET = withTrustedAuthIngress(
  handlers.GET,
  trustedAuthProxySecret,
);
export const POST = withTrustedAuthIngress(
  withBoundedRequestBody(handlers.POST, 64 * 1024, 5_000),
  trustedAuthProxySecret,
);
