import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,

  env: {
    NEXT_PUBLIC_WS_URL:      process.env.NEXT_PUBLIC_WS_URL      ?? "ws://localhost:8000/ws/frontend",
    NEXT_PUBLIC_BACKEND_URL: process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://localhost:8000",
  },

  // Proxy all /api/* and /ws/* calls to the FastAPI backend
  async rewrites() {
    const backend = process.env.BACKEND_URL ?? "http://localhost:8000";
    return [
      { source: "/api/:path*", destination: `${backend}/api/:path*` },
      { source: "/ws/:path*",  destination: `${backend}/ws/:path*`  },
    ];
  },

  async headers() {
    const isDev = process.env.NODE_ENV !== "production";

    // Tile CDNs used by react-leaflet
    const tileDomains = [
      "https://*.basemaps.cartocdn.com",   // CartoDB Dark Matter
      "https://*.tile.openstreetmap.org",  // OSM fallback
      "https://*.tile.openstreetmap.fr",
    ].join(" ");

    return [
      {
        source: "/(.*)",
        headers: [
          {
            key: "Content-Security-Policy",
            value: [
              "default-src 'self'",
              "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
              "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
              // Tile images must be whitelisted — CartoDB + OSM
              `img-src 'self' data: blob: ${tileDomains}`,
              // Tile HTTP requests + WS + REST
              `connect-src 'self' ${tileDomains} ${isDev ? "http://localhost:8000 http://localhost:3000 " : ""}ws://localhost:8000 wss://localhost:8000`,
              "font-src 'self' https://fonts.gstatic.com",
              "worker-src 'self' blob:",
            ].join("; "),
          },
        ],
      },
    ];
  },
};

export default nextConfig;
