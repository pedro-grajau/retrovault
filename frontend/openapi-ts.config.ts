import { defineConfig } from "@hey-api/openapi-ts"

export default defineConfig({
  input: process.env.OPENAPI_INPUT ?? "./openapi.json",
  output: process.env.OPENAPI_OUTPUT ?? "./src/client",

  plugins: [
    { name: "@hey-api/client-axios", throwOnError: true },
    { name: "@hey-api/typescript", case: "preserve" },
    {
      name: "@hey-api/sdk",
      operations: {
        // NOTE: this doesn't allow tree-shaking
        strategy: "byTags",
        methods: "static",
        containerName: "{{name}}Service",
        methodName: (name: string): string => name.replace(/^[^-]*-/, ""),
      },
    },
  ],
})
