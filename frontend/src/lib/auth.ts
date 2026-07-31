import "server-only";
import { betterAuth } from "better-auth";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { Pool } from "pg";
import {
  googleSignupPolicy,
  readAuthRuntimeConfig,
  readDatabaseRuntimeConfig,
  withDatabaseCaFallback,
} from "@/lib/security-config";
import { TRUSTED_AUTH_CLIENT_IP_HEADER } from "@/lib/bounded-route";

const bundledDatabaseCa = readFileSync(
  resolve(process.cwd(), "certs", "supabase-root-2021.crt"),
  "utf8",
);
const databaseConfig = readDatabaseRuntimeConfig(
  withDatabaseCaFallback(process.env, bundledDatabaseCa),
);
const authConfig = readAuthRuntimeConfig(process.env);

export const trustedAppOrigin = authConfig.trustedOrigin;
export const emailSignupEnabled = authConfig.allowEmailSignup;
export const googleSignupEnabled = authConfig.allowGoogleSignup;
export const trustedAuthProxySecret = authConfig.trustedProxySecret;

export const db = new Pool({
  connectionString: databaseConfig.connectionString,
  ssl: databaseConfig.ssl,
  max: 10,
  connectionTimeoutMillis: 5_000,
  idleTimeoutMillis: 30_000,
  statement_timeout: 10_000,
  query_timeout: 12_000,
});

export const auth = betterAuth({
  database: db,
  baseURL: authConfig.baseURL,
  secret: authConfig.secret,
  trustedOrigins: [authConfig.trustedOrigin],
  advanced: {
    ipAddress: {
      // The route wrapper discards public forwarding headers and creates this
      // single-value header only for a verified ingress request.
      ipAddressHeaders: [TRUSTED_AUTH_CLIENT_IP_HEADER],
      trustedProxies: [],
    },
  },
  rateLimit: {
    enabled: true,
    window: 60,
    max: 60,
    customRules: {
      "/sign-in/email": {
        window: 60,
        max: 5,
      },
      "/sign-up/email": {
        window: 3_600,
        max: 3,
      },
    },
  },
  socialProviders: {
    google: {
      clientId: authConfig.googleClientId,
      clientSecret: authConfig.googleClientSecret,
      ...googleSignupPolicy(authConfig.allowGoogleSignup),
    },
  },
  emailAndPassword: {
    enabled: true,
    disableSignUp: !authConfig.allowEmailSignup,
  },
});
