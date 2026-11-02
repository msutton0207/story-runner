FROM node:20-alpine
WORKDIR /srv
COPY server.js README.md ./
COPY public/ public/
# listen on all interfaces inside the container (the Mac version stays on 127.0.0.1)
ENV HOST=0.0.0.0
EXPOSE 3100
CMD ["node", "server.js"]
