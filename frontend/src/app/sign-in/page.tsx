import { Suspense } from "react";
import { readAuthRuntimeConfig } from "@/lib/security-config";
import SignInCard from "./sign-in-card";

// The card itself is a client component, so it cannot read the server's own
// view of which providers are configured. Resolving that here and passing it
// down keeps the button and the provider registration in src/lib/auth.ts from
// disagreeing. Reading the config directly rather than importing "@/lib/auth"
// avoids opening that module's connection pool just to render a form.
export default function SignInPage() {
  const { allowGoogleSignup } = readAuthRuntimeConfig(process.env);

  return (
    <Suspense fallback={null}>
      <SignInCard googleEnabled={allowGoogleSignup} />
    </Suspense>
  );
}
