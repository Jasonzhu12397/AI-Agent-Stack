FROM node:22-alpine
WORKDIR /workspace
COPY apps/web-studio/package.json ./
RUN npm install --no-audit --no-fund
COPY apps/web-studio ./
EXPOSE 5173
CMD ["npm", "run", "dev"]
