# syntax=docker/dockerfile:1.7
#
# Multi-stage. The runtime stage carries Next's standalone output, so no
# node_modules ship in the final image.
#
# There is deliberately NO build-time backend URL. Client components call the
# relative /bff/* proxy, and the route handler reads BACKEND_INTERNAL_URL at
# request time — so this one image promotes unchanged from local to dev to prod.
# A NEXT_PUBLIC_API_URL here would be inlined into the JS bundle at `next build`
# and pin the image to one environment (see ADR-001).

# ------------------------------------------------------------------ deps
FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

# ----------------------------------------------------------------- build
FROM node:22-slim AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

# --------------------------------------------------------- dev (compose)
# Used by docker-compose for hot reload; never deployed.
FROM node:22-slim AS dev
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /app/node_modules ./node_modules
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev"]

# ---------------------------------------------------------------- runtime
FROM node:22-slim AS runtime
WORKDIR /app

ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Non-root. ECS will not enforce this for you.
RUN useradd --create-home --uid 10001 nextjs

# `standalone` bundles the server and only the modules it actually uses; static
# and public are not included in it and must be copied alongside.
COPY --from=build --chown=nextjs:nextjs /app/.next/standalone ./
COPY --from=build --chown=nextjs:nextjs /app/.next/static ./.next/static

USER nextjs
EXPOSE 3000

# node:*-slim has no curl, so the usual `curl -f` healthcheck fails in a way that
# looks like the app is down.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD node -e "fetch('http://127.0.0.1:3000/').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "server.js"]
