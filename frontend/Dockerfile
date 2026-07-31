# Digest-pinned; bumped by the `docker` ecosystem in .github/dependabot.yml.
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293

WORKDIR /app

# `npm ci` (not `npm install`) so the dev image installs exactly the audited lockfile.
# Requires package-lock.json to be copied alongside package.json.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# The node images ship a non-root `node` user (uid 1000). Use it rather than root.
RUN chown -R node:node /app
USER node

EXPOSE 5173

CMD ["npm", "run", "dev"]
