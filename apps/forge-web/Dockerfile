# syntax=docker/dockerfile:1
# Built from the repository root: docker build -f apps/forge-web/Dockerfile .
FROM node:24-alpine AS build
WORKDIR /app
COPY apps/forge-web/package.json apps/forge-web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY apps/forge-web/ ./
# Vite inlines VITE_* variables into the bundle, so the API URL is fixed per image.
ARG VITE_API_URL=
ENV VITE_API_URL=$VITE_API_URL
# Local stack only: a development bearer token, also inlined. Leave it empty
# for any image that leaves your machine.
ARG VITE_API_TOKEN=
ENV VITE_API_TOKEN=$VITE_API_TOKEN
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.28-alpine
COPY apps/forge-web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
