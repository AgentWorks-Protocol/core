/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // The dashboard reads verified proof artifacts (web/data/**) from the server at request time via
  // dynamic fs reads (readdirSync), which Next's tracer can't detect — so bundle them explicitly.
  // Without this, production falls back to the sibling ../agents/scripts/.market/runs snapshot.
  outputFileTracingIncludes: {
    "/": ["./data/**"],
    "/dashboard": ["./data/**"],
    "/dashboard/jobs/[jobId]": ["./data/**"],
    "/dashboard/proofs": ["./data/**"],
    "/dashboard/flow": ["./data/**"],
  },
  // No ESLint config is shipped (bespoke demo); keep TS type-checking on, skip lint in builds.
  eslint: { ignoreDuringBuilds: true },
};

export default nextConfig;
