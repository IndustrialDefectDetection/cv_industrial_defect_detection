type Environment = Readonly<Record<string, string | undefined>>;

type VerifiedTlsOptions = {
  ca: string;
  rejectUnauthorized: true;
};

export type DatabaseRuntimeConfig = {
  connectionString: string;
  ssl: false | VerifiedTlsOptions;
};

export type AuthRuntimeConfig = {
  allowEmailSignup: boolean;
  allowGoogleSignup: boolean;
  baseURL: string;
  googleClientId: string;
  googleClientSecret: string;
  secret: string;
  trustedProxySecret: string | null;
  trustedOrigin: string;
};

export type MesBackendRuntimeConfig = {
  baseURL: string;
  internalApiToken: string;
};

export function googleSignupPolicy(allowSignup: boolean): {
  disableImplicitSignUp: boolean;
  disableSignUp: boolean;
} {
  return {
    disableImplicitSignUp: !allowSignup,
    disableSignUp: !allowSignup,
  };
}

const LOOPBACK_HOSTS = new Set([
  "127.0.0.1",
  "::1",
  "[::1]",
  "localhost",
]);

const MES_USER_ID_PATTERN = /^[A-Za-z0-9._:-]{1,200}$/;

function requireEnvironmentValue(
  environment: Environment,
  name: string,
): string {
  const value = environment[name]?.trim();

  if (!value) {
    throw new Error(`${name} is required`);
  }

  return value;
}

function requireSecret(
  environment: Environment,
  name: string,
  minimumLength = 32,
): string {
  const value = requireEnvironmentValue(environment, name);

  if (value.length < minimumLength) {
    throw new Error(`${name} must be at least ${minimumLength} characters`);
  }

  return value;
}

function parseAbsoluteUrl(name: string, value: string): URL {
  let url: URL;

  try {
    url = new URL(value);
  } catch {
    throw new Error(`${name} must be a valid absolute URL`);
  }

  if (url.username || url.password) {
    throw new Error(`${name} must not contain URL credentials`);
  }

  if (url.search || url.hash) {
    throw new Error(`${name} must not contain a query string or fragment`);
  }

  return url;
}

function isLoopbackHost(hostname: string): boolean {
  return LOOPBACK_HOSTS.has(hostname.toLowerCase());
}

function parseHttpServiceOrigin(name: string, value: string): string {
  const url = parseAbsoluteUrl(name, value);

  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error(`${name} must use http or https`);
  }

  if (url.protocol !== "https:" && !isLoopbackHost(url.hostname)) {
    throw new Error(`${name} must use https for non-loopback hosts`);
  }

  if (url.pathname !== "/") {
    throw new Error(`${name} must contain an origin without a path`);
  }

  return url.origin;
}

function parseStrictBoolean(
  environment: Environment,
  name: string,
  defaultValue: boolean,
): boolean {
  const value = environment[name]?.trim().toLowerCase();

  if (!value) {
    return defaultValue;
  }

  if (value === "true") {
    return true;
  }

  if (value === "false") {
    return false;
  }

  throw new Error(`${name} must be either true or false`);
}

function normalizeCertificate(value: string): string {
  return value.replace(/\\n/g, "\n").trim();
}

export function withDatabaseCaFallback(
  environment: Environment,
  fallbackCertificate: string,
): Environment {
  if (environment.DATABASE_CA_CERT?.trim()) {
    return environment;
  }

  return {
    ...environment,
    DATABASE_CA_CERT: fallbackCertificate,
  };
}

export function readDatabaseRuntimeConfig(
  environment: Environment,
): DatabaseRuntimeConfig {
  const connectionString = requireEnvironmentValue(
    environment,
    "DATABASE_URL",
  );
  let url: URL;

  try {
    url = new URL(connectionString);
  } catch {
    throw new Error("DATABASE_URL must be a valid PostgreSQL URL");
  }

  if (!["postgres:", "postgresql:"].includes(url.protocol)) {
    throw new Error("DATABASE_URL must use the postgres or postgresql scheme");
  }

  if (!url.hostname || !url.username || !url.password || url.pathname === "/") {
    throw new Error(
      "DATABASE_URL must include a host, database, username, and password",
    );
  }

  const firstQueryParameter = url.searchParams.keys().next().value;
  if (firstQueryParameter !== undefined) {
    throw new Error(
      `DATABASE_URL must not set ${firstQueryParameter}; connection options `
      + "are configured by the server",
    );
  }

  if (isLoopbackHost(url.hostname)) {
    return {
      connectionString,
      ssl: false,
    };
  }

  const ca = normalizeCertificate(
    requireEnvironmentValue(environment, "DATABASE_CA_CERT"),
  );

  if (
    !ca.startsWith("-----BEGIN CERTIFICATE-----")
    || !ca.endsWith("-----END CERTIFICATE-----")
  ) {
    throw new Error("DATABASE_CA_CERT must contain a PEM certificate");
  }

  return {
    connectionString,
    ssl: {
      ca,
      rejectUnauthorized: true,
    },
  };
}

export function readAuthRuntimeConfig(
  environment: Environment,
): AuthRuntimeConfig {
  const baseURL = parseHttpServiceOrigin(
    "BETTER_AUTH_URL",
    requireEnvironmentValue(environment, "BETTER_AUTH_URL"),
  );

  const remoteOrigin = !isLoopbackHost(new URL(baseURL).hostname);

  return {
    allowEmailSignup: parseStrictBoolean(
      environment,
      "AUTH_ALLOW_EMAIL_SIGNUP",
      true,
    ),
    allowGoogleSignup: parseStrictBoolean(
      environment,
      "AUTH_ALLOW_GOOGLE_SIGNUP",
      true,
    ),
    baseURL,
    googleClientId: requireEnvironmentValue(
      environment,
      "GOOGLE_CLIENT_ID",
    ),
    googleClientSecret: requireSecret(
      environment,
      "GOOGLE_CLIENT_SECRET",
      16,
    ),
    secret: requireSecret(environment, "BETTER_AUTH_SECRET"),
    trustedProxySecret: remoteOrigin
      ? requireSecret(environment, "AUTH_TRUSTED_PROXY_SECRET")
      : null,
    trustedOrigin: baseURL,
  };
}

export function readMesBackendRuntimeConfig(
  environment: Environment,
): MesBackendRuntimeConfig {
  return {
    baseURL: parseHttpServiceOrigin(
      "BACKEND_URL",
      requireEnvironmentValue(environment, "BACKEND_URL"),
    ),
    internalApiToken: requireSecret(
      environment,
      "MES_INTERNAL_API_TOKEN",
    ),
  };
}

export function isValidMesUserId(value: string): boolean {
  return MES_USER_ID_PATTERN.test(value);
}
