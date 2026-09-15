FROM node:22-alpine AS build
WORKDIR /app
COPY package.json tsconfig.json index.html ./
COPY src.tsx styles.css ./
RUN npm install && npm run build
FROM nginx:1.29-alpine
COPY --from=build /app/dist /usr/share/nginx/html

