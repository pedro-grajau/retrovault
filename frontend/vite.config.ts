import react from "@vitejs/plugin-react-swc"
import { defineConfig } from "vite"

export default defineConfig({
  build: {
    outDir: process.env.VITE_BUILD_OUT_DIR ?? "../backend/app/frontend",
    emptyOutDir: true,
  },
  plugins: [react()],
})
