# syntax=docker/dockerfile:1
FROM node:26-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5 AS builder
WORKDIR /opt/xb3-telemetry
COPY rootfs/opt/xb3-telemetry/package*.json ./
RUN npm ci --ignore-scripts
COPY rootfs/opt/xb3-telemetry/tsconfig.json ./
COPY rootfs/opt/xb3-telemetry/src ./src
COPY rootfs/opt/xb3-telemetry/test ./test
RUN npm run check && npm test && npm run build && npm prune --omit=dev --ignore-scripts

FROM node:26-alpine@sha256:6bd9ab43c0ec0190ca7b2d193906cb8b7edf6e7f4afa43ddeccd6452710bffb5
WORKDIR /opt/xb3-telemetry
COPY --from=builder /opt/xb3-telemetry/package*.json ./
COPY --from=builder /opt/xb3-telemetry/node_modules ./node_modules
COPY --from=builder /opt/xb3-telemetry/dist ./dist
EXPOSE 19090
ENTRYPOINT ["node", "dist/main.js"]
LABEL org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps"
