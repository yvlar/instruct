FROM node:26-alpine AS build
WORKDIR /app
COPY package.json package-lock.json tsconfig.json index.html vite-env.d.ts ./
COPY src.tsx styles.css ./
RUN npm ci && npm run build
FROM nginx:1.29-alpine
COPY --from=build /app/dist /usr/share/nginx/html
