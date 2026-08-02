// Creates the tables the frontend needs, in the same PostgreSQL database the
// rest of the pipeline uses.
//
// Nothing else in the repo did this. Better Auth ships no schema of its own -
// it derives one from its options and expects you to apply it - so a fresh
// database had no "user", "session", "account" or "verification" table, and
// every sign-up and sign-in failed at the first query. The browser only showed
// "Authentication failed", which reads like a wrong password rather than a
// missing table. database/chat-history.sql had the same problem from the other
// end: it declares a foreign key to "user"(id) and had no runner at all.
//
// Both halves are idempotent, so this is safe to run on every launch: Better
// Auth creates only the tables that are absent, and the chat history statements
// are all IF NOT EXISTS.

import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { Pool } from "pg";
import { getMigrations } from "better-auth/db/migration";
import { readDatabaseRuntimeConfig } from "../src/lib/security-config.ts";

const FRONTEND_DIRECTORY = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
);

// Only the fields Better Auth reads to derive a schema. This deliberately does
// not import src/lib/auth.ts: that module is marked "server-only" and opens a
// connection pool as a side effect of being imported. Social providers add no
// tables - an OAuth identity is a row in "account" - so the schema produced
// here is the same one the running server expects, with or without Google.
function schemaOptions(pool) {
  return {
    database: pool,
    emailAndPassword: { enabled: true },
  };
}

async function migrateAuthenticationTables(pool) {
  const { toBeCreated, toBeAdded, runMigrations } = await getMigrations(
    schemaOptions(pool),
  );

  if (toBeCreated.length === 0 && toBeAdded.length === 0) {
    console.log("Authentication tables are already up to date");
    return;
  }

  const created = toBeCreated.map((table) => table.table);
  const altered = toBeAdded.map((table) => table.table);

  await runMigrations();

  if (created.length > 0) {
    console.log(`Created authentication tables: ${created.join(", ")}`);
  }
  if (altered.length > 0) {
    console.log(`Updated authentication tables: ${altered.join(", ")}`);
  }
}

async function migrateChatHistoryTables(pool) {
  const statements = await readFile(
    resolve(FRONTEND_DIRECTORY, "database/chat-history.sql"),
    "utf8",
  );

  // One call, so the whole file is a single implicit transaction: a checkout
  // that fails halfway does not leave "conversation" without "message".
  await pool.query(statements);
  console.log("Chat history tables are up to date");
}

async function migrate() {
  // The same reader the server uses, so this script cannot connect on terms
  // the server would reject - notably TLS verification for a non-loopback host.
  const databaseConfig = readDatabaseRuntimeConfig(process.env);
  const pool = new Pool({
    connectionString: databaseConfig.connectionString,
    ssl: databaseConfig.ssl,
    max: 1,
    connectionTimeoutMillis: 10_000,
  });

  try {
    await migrateAuthenticationTables(pool);
    await migrateChatHistoryTables(pool);
  } finally {
    await pool.end();
  }
}

migrate().catch((error) => {
  console.error(`Database migration failed: ${error.message}`);
  process.exitCode = 1;
});
