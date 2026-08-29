FROM node:22-alpine

ENV NODE_ENV=production
# In a container the port must be reachable from outside the container itself;
# the public edge is expected to be an HTTPS proxy, not this process.
ENV BIND_ADDRESS=0.0.0.0

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev

COPY health_call_nudger.js api.json config.json ./

# State lives on a mounted volume so it survives restarts and rebuilds.
ENV STATE_PATH=/app/data/state.json

RUN mkdir -p logs data && chown -R node:node /app
USER node

EXPOSE 4300

CMD ["node", "health_call_nudger.js"]
