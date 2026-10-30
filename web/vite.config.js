import { defineConfig } from "vite";

// Dev server only. /api is forwarded to uvicorn so the page
// and the API look like one origin to the browser.
export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
