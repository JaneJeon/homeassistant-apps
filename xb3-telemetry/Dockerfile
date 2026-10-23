# syntax=docker/dockerfile:1
FROM node:24-alpine@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53 AS builder
WORKDIR /opt/xb3-telemetry
COPY rootfs/opt/xb3-telemetry/package*.json ./
RUN npm ci --ignore-scripts
COPY rootfs/opt/xb3-telemetry/tsconfig.json ./
COPY rootfs/opt/xb3-telemetry/src ./src
COPY rootfs/opt/xb3-telemetry/test ./test
RUN npm run check && npm test && npm run build && npm prune --omit=dev --ignore-scripts

FROM node:24-alpine@sha256:06d7adf3e0ef61a13f1d28682726366f97a15aa1888c934d3ecec86c33918b53
WORKDIR /opt/xb3-telemetry
COPY --from=builder /opt/xb3-telemetry/package*.json ./
COPY --from=builder /opt/xb3-telemetry/node_modules ./node_modules
COPY --from=builder /opt/xb3-telemetry/dist ./dist
EXPOSE 19090
ENTRYPOINT ["node", "dist/main.js"]
LABEL org.opencontainers.image.source="https://github.com/JaneJeon/homeassistant-apps"
