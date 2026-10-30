import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// In development, API calls are proxied to a locally running web backend.
export default defineConfig({
  plugins: [vue()],
  build: { outDir: "dist", sourcemap: false },
  server: {
    proxy: {
      "/api": "http://localhost:8080",
    },
  },
});
