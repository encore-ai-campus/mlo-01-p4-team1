# Pinned Node major and pnpm toolchain; install dependencies from the lockfile.
FROM node:24-bookworm-slim

ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
RUN npm install --global "pnpm@10.19.0"
WORKDIR /workspace/app/frontend
COPY --chown=node:node frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY --chown=node:node frontend/ ./
# Vite bundles its TS config beside vite.config.ts; the workdir must be writable.
RUN chown -R node:node /workspace/app/frontend
USER node

EXPOSE 5173
CMD ["pnpm", "exec", "vite", "--host", "0.0.0.0", "--port", "5173", "--strictPort"]
