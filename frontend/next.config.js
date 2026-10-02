/** @type {import('next').NextConfig} */
const nextConfig = {
  // Docker 部署使用 standalone 输出，镜像只需携带 .next/standalone。
  output: "standalone",
  // Keep the dev server output separate from production build artifacts.
  distDir: process.env.NODE_ENV === "development" ? ".next-dev" : ".next",
  env: {
    NEXT_PUBLIC_API_BASE_URL: process.env.NEXT_PUBLIC_API_BASE_URL || "http://localhost:8000",
    NEXT_PUBLIC_API_KEY: process.env.NEXT_PUBLIC_API_KEY || ""
  }
};

module.exports = nextConfig;
