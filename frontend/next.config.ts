import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Produces a self-contained server bundle, so the runtime image does not need
  // node_modules. Smaller image, faster ECS task start.
  output: "standalone",

  // `next dev` otherwise writes AGENTS.md and CLAUDE.md into this directory on
  // every start. They are generated boilerplate, and a CLAUDE.md here would be
  // loaded as instructions alongside the repository's real one.
  agentRules: false,

  // NOTE: deliberately NO rewrites() proxying to the backend. Rewrites are
  // evaluated at build time and frozen into routes-manifest.json, so a
  // destination read from the environment is fixed at build rather than at
  // request time — exactly the trap the BFF route handler exists to avoid.
  // See app/bff/[...path]/route.ts.
};

export default nextConfig;
