import assert from "node:assert/strict";
import { chmod, mkdtemp, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import {
  sanitizeNextEnvironment,
  secureNextArguments,
  validateNextEnvironmentFiles,
} from "../scripts/run-next-secure.mjs";


test("Next.js development and production listeners are loopback-only", async () => {
  const packageUrl = new URL("../package.json", import.meta.url);
  const packageJson = JSON.parse(
    await readFile(packageUrl, "utf8"),
  ) as { scripts?: Record<string, string> };

  assert.equal(
    packageJson.scripts?.dev,
    "node scripts/run-next-secure.mjs dev",
  );
  assert.equal(
    packageJson.scripts?.start,
    "node scripts/run-next-secure.mjs start",
  );
  assert.equal(
    packageJson.scripts?.build,
    "node scripts/run-next-secure.mjs build",
  );
  assert.deepEqual(
    secureNextArguments("dev", ["--port", "3001"]),
    ["dev", "--port", "3001", "--hostname", "127.0.0.1"],
  );
  assert.throws(
    () => secureNextArguments("start", ["--hostname=0.0.0.0"]),
    /cannot be overridden/,
  );
});


test("Next.js children cannot inherit ambient proxy or CA overrides", () => {
  const sanitized = sanitizeNextEnvironment({
    AUTH_TRUSTED_PROXY_SECRET: "application-secret",
    DATABASE_URL: "postgresql://application-secret",
    HTTPS_PROXY: "http://attacker.invalid:8080",
    http_proxy: "http://attacker.invalid:8080",
    NODE_EXTRA_CA_CERTS: "/tmp/attacker-ca.pem",
    NODE_DEBUG: "http,tls",
    NODE_OPTIONS: "--use-env-proxy",
    NODE_TLS_REJECT_UNAUTHORIZED: "0",
    NODE_USE_ENV_PROXY: "1",
    OPENSSL_CONF: "/tmp/attacker-openssl.cnf",
    PATH: "/safe/bin",
  });

  assert.equal(sanitized.AUTH_TRUSTED_PROXY_SECRET, "application-secret");
  assert.equal(sanitized.DATABASE_URL, "postgresql://application-secret");
  assert.equal(sanitized.PATH, "/safe/bin");
  assert.equal(sanitized.HTTPS_PROXY, undefined);
  assert.equal(sanitized.http_proxy, undefined);
  assert.equal(sanitized.NODE_EXTRA_CA_CERTS, undefined);
  assert.equal(sanitized.NODE_DEBUG, undefined);
  assert.equal(sanitized.NODE_OPTIONS, undefined);
  assert.equal(sanitized.NODE_TLS_REJECT_UNAUTHORIZED, undefined);
  assert.equal(sanitized.NODE_USE_ENV_PROXY, undefined);
  assert.equal(sanitized.OPENSSL_CONF, undefined);
});


test("Next.js rejects exposed or symlinked secret files", async () => {
  const directory = await mkdtemp(join(tmpdir(), "mes-next-env-"));
  const envFile = join(directory, ".env.local");
  try {
    await writeFile(envFile, "BETTER_AUTH_SECRET=not-real\n", { mode: 0o600 });
    validateNextEnvironmentFiles(directory, "dev", "darwin");

    await chmod(envFile, 0o644);
    assert.throws(
      () => validateNextEnvironmentFiles(directory, "dev", "darwin"),
      /chmod 600/,
    );

    await rm(envFile);
    await symlink(join(directory, "missing-target"), envFile);
    assert.throws(
      () => validateNextEnvironmentFiles(directory, "dev", "darwin"),
      /non-regular/,
    );
  } finally {
    await rm(directory, { force: true, recursive: true });
  }
});
