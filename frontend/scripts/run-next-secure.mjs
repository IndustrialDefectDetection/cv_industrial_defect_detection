import { spawn } from "node:child_process";
import { lstatSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";


const BLOCKED_ENVIRONMENT_NAMES = new Set([
  "all_proxy",
  "global_agent_http_proxy",
  "global_agent_https_proxy",
  "http_proxy",
  "https_proxy",
  "node_extra_ca_certs",
  "node_debug",
  "node_debug_native",
  "node_options",
  "node_tls_reject_unauthorized",
  "node_use_env_proxy",
  "no_proxy",
  "npm_config_https_proxy",
  "npm_config_noproxy",
  "npm_config_proxy",
  "openssl_conf",
  "sslkeylogfile",
  "ssl_cert_dir",
  "ssl_cert_file",
]);

export function sanitizeNextEnvironment(source) {
  return Object.fromEntries(
    Object.entries(source).filter(
      ([name]) => !BLOCKED_ENVIRONMENT_NAMES.has(name.toLowerCase()),
    ),
  );
}

export function secureNextArguments(mode, extraArguments = []) {
  if (!["build", "dev", "start"].includes(mode)) {
    throw new Error("Next.js mode must be build, dev, or start");
  }
  if (
    extraArguments.some(
      (argument) =>
        argument === "-H"
        || argument.startsWith("-H=")
        || argument === "--hostname"
        || argument.startsWith("--hostname="),
    )
  ) {
    throw new Error("The loopback hostname cannot be overridden");
  }
  if (mode === "build") {
    return [mode, ...extraArguments];
  }
  return [mode, ...extraArguments, "--hostname", "127.0.0.1"];
}

export function validateNextEnvironmentFiles(
  frontendDirectory,
  mode,
  platform = process.platform,
) {
  const environmentName = mode === "dev" ? "development" : "production";
  const candidates = [
    ".env",
    ".env.local",
    `.env.${environmentName}`,
    `.env.${environmentName}.local`,
  ];

  for (const basename of candidates) {
    const path = resolve(frontendDirectory, basename);
    let file;
    try {
      file = lstatSync(path);
    } catch (error) {
      if (error?.code === "ENOENT") {
        continue;
      }
      throw error;
    }
    if (file.isSymbolicLink() || !file.isFile()) {
      throw new Error(`Refusing to load non-regular ${basename}`);
    }
    if (platform === "win32") {
      throw new Error(
        `${basename} cannot be permission-verified on Windows; `
        + "supply its values through the process environment",
      );
    }
    if ((file.mode & 0o077) !== 0) {
      throw new Error(
        `${basename} must be owner-only; run chmod 600 on the file`,
      );
    }
  }
}

function run() {
  const mode = process.argv[2];
  const frontendDirectory = resolve(
    dirname(fileURLToPath(import.meta.url)),
    "..",
  );
  validateNextEnvironmentFiles(frontendDirectory, mode);
  const nextBinary = resolve(
    frontendDirectory,
    "node_modules/next/dist/bin/next",
  );
  const child = spawn(
    process.execPath,
    [nextBinary, ...secureNextArguments(mode, process.argv.slice(3))],
    {
      env: sanitizeNextEnvironment(process.env),
      shell: false,
      stdio: "inherit",
    },
  );
  child.on("error", () => {
    process.exitCode = 1;
  });
  child.on("exit", (code) => {
    process.exitCode = code ?? 1;
  });
}

if (
  process.argv[1]
  && resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  run();
}
