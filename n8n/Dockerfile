FROM n8nio/n8n:2.19.2

# Railway volumes mount as root — run as root to avoid permission issues
USER root

ENV N8N_PORT=5678
ENV NODE_ENV=production
ENV N8N_DIAGNOSTICS_ENABLED=false
ENV GENERIC_TIMEZONE=UTC
ENV N8N_USER_FOLDER=/home/node/.n8n

EXPOSE 5678
