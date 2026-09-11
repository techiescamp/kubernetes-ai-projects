import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Produces a minimal `.next/standalone` server bundle for the container image (frontend/Dockerfile).
  // The browser's relative /api/* calls are proxied to the real backend by
  // app/api/[...path]/route.ts, not by rewrites() here - rewrites() gets baked into the
  // standalone build's routing manifest at `next build` time, so it can't read a runtime-only
  // BACKEND_URL env var (confirmed via a real deployed test - see SPEC.md).
  output: "standalone",
};

export default nextConfig;
