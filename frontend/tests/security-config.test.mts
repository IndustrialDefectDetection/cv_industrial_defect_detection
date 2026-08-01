import assert from "node:assert/strict";
import test from "node:test";
import {
  googleSignupPolicy,
  isValidMesUserId,
  readAuthRuntimeConfig,
  readDatabaseRuntimeConfig,
  readMesBackendRuntimeConfig,
} from "../src/lib/security-config.ts";

const certificate = [
  "-----BEGIN CERTIFICATE-----",
  "dGVzdA==",
  "-----END CERTIFICATE-----",
].join("\n");

const validAuthEnvironment = {
  BETTER_AUTH_SECRET: "a".repeat(32),
  BETTER_AUTH_URL: "http://localhost:3000",
  GOOGLE_CLIENT_ID: "google-client-id",
  GOOGLE_CLIENT_SECRET: "google-client-secret",
};

test("local PostgreSQL can run without TLS", () => {
  const config = readDatabaseRuntimeConfig({
    DATABASE_URL: "postgresql://user:password@127.0.0.1:5432/app",
  });

  assert.equal(config.ssl, false);
});

test("remote PostgreSQL requires a CA and verified TLS", () => {
  assert.throws(
    () =>
      readDatabaseRuntimeConfig({
        DATABASE_URL: "postgresql://user:password@db.example.com:5432/app",
      }),
    /DATABASE_CA_CERT is required/,
  );

  const config = readDatabaseRuntimeConfig({
    DATABASE_CA_CERT: certificate,
    DATABASE_URL: "postgresql://user:password@db.example.com:5432/app",
  });

  assert.deepEqual(config.ssl, {
    ca: certificate,
    rejectUnauthorized: true,
  });
});

test("database URLs cannot override server connection policy", () => {
  assert.throws(
    () =>
      readDatabaseRuntimeConfig({
        DATABASE_URL:
          "postgresql://user:password@127.0.0.1:5432/app?sslmode=disable",
      }),
    /must not set sslmode/,
  );

  assert.throws(
    () =>
      readDatabaseRuntimeConfig({
        DATABASE_URL:
          "postgresql://user:password@127.0.0.1:5432/app?SSLMODE=disable",
      }),
    /must not set SSLMODE/,
  );

  assert.throws(
    () =>
      readDatabaseRuntimeConfig({
        DATABASE_URL:
          "postgresql://user:password@127.0.0.1:5432/app"
          + "?host=remote.example.com",
      }),
    /must not set host/,
  );

  assert.throws(
    () =>
      readDatabaseRuntimeConfig({
        DATABASE_URL:
          "postgresql://user:password@127.0.0.1:5432/app"
          + "?password=overridden",
      }),
    /must not set password/,
  );
});

test("authentication defaults to open registration and supports explicit closure", () => {
  assert.equal(
    readAuthRuntimeConfig(validAuthEnvironment).allowEmailSignup,
    true,
  );
  assert.equal(
    readAuthRuntimeConfig({
      ...validAuthEnvironment,
      AUTH_ALLOW_EMAIL_SIGNUP: "false",
    }).allowEmailSignup,
    false,
  );
  assert.equal(
    readAuthRuntimeConfig(validAuthEnvironment).allowGoogleSignup,
    true,
  );
  assert.equal(
    readAuthRuntimeConfig({
      ...validAuthEnvironment,
      AUTH_ALLOW_GOOGLE_SIGNUP: "false",
    }).allowGoogleSignup,
    false,
  );
  assert.deepEqual(googleSignupPolicy(false), {
    disableImplicitSignUp: true,
    disableSignUp: true,
  });
});

test("authentication rejects weak secrets and insecure remote origins", () => {
  assert.throws(
    () =>
      readAuthRuntimeConfig({
        ...validAuthEnvironment,
        BETTER_AUTH_SECRET: "too-short",
      }),
    /at least 32 characters/,
  );

  assert.throws(
    () =>
      readAuthRuntimeConfig({
        ...validAuthEnvironment,
        BETTER_AUTH_URL: "http://app.example.com",
      }),
    /must use https/,
  );

  assert.throws(
    () =>
      readAuthRuntimeConfig({
        ...validAuthEnvironment,
        BETTER_AUTH_URL: "https://app.example.com",
      }),
    /AUTH_TRUSTED_PROXY_SECRET is required/,
  );

  const remoteConfig = readAuthRuntimeConfig({
    ...validAuthEnvironment,
    AUTH_TRUSTED_PROXY_SECRET: "p".repeat(32),
    BETTER_AUTH_URL: "https://app.example.com",
  });
  assert.equal(remoteConfig.trustedProxySecret, "p".repeat(32));
});

test("backend configuration requires an authenticated HTTPS service", () => {
  assert.throws(
    () =>
      readMesBackendRuntimeConfig({
        BACKEND_URL: "http://backend.example.com",
        MES_INTERNAL_API_TOKEN: "t".repeat(32),
      }),
    /must use https/,
  );

  assert.throws(
    () =>
      readMesBackendRuntimeConfig({
        BACKEND_URL: "http://127.0.0.1:8000",
        MES_INTERNAL_API_TOKEN: "too-short",
      }),
    /at least 32 characters/,
  );

  assert.deepEqual(
    readMesBackendRuntimeConfig({
      BACKEND_URL: "http://127.0.0.1:8000",
      MES_INTERNAL_API_TOKEN: "t".repeat(32),
    }),
    {
      baseURL: "http://127.0.0.1:8000",
      internalApiToken: "t".repeat(32),
    },
  );
});

test("MES user IDs match the backend ownership-header contract", () => {
  assert.equal(
    isValidMesUserId("550e8400-e29b-41d4-a716-446655440000"),
    true,
  );
  assert.equal(isValidMesUserId("user.name:team_1"), true);
  assert.equal(isValidMesUserId("user/name"), false);
  assert.equal(isValidMesUserId("x".repeat(201)), false);
});
