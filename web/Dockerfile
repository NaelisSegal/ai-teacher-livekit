# The class page. Built from the top of the repo (see docker-compose.yml):
#   docker build -f web/Dockerfile .
# Lessons and pictures made in class are folders mounted at /app/lessons and
# /app/generated. The site's name is set when the image is built.
FROM node:22-slim
RUN corepack enable && corepack prepare pnpm@10.33.0 --activate
WORKDIR /app/web
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY web/ ./
ARG NEXT_PUBLIC_SITE_NAME=""
ENV NEXT_PUBLIC_SITE_NAME=$NEXT_PUBLIC_SITE_NAME NEXT_TELEMETRY_DISABLED=1
RUN pnpm build
EXPOSE 4072
CMD ["pnpm", "start", "--port", "4072"]
