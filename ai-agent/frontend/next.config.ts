import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Produces a minimal `.next/standalone` server bundle for the container image (frontend/Dockerfile).
  output: "standalone",
};

export default nextConfig;
