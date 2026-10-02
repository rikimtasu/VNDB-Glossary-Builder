import { defineConfig } from "vite";

// Keep the dev-server port fixed so `tauri dev` can attach to it.
export default defineConfig({
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
  },
  build: {
    target: "chrome105",
  },
});
