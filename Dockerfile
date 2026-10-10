# DayanVPN Panel - Xray + nginx + Telegram admin bot
FROM alpine:3.20

RUN apk add --no-cache nginx curl unzip ca-certificates python3

# Latest Xray-core
RUN mkdir -p /usr/local/bin/xray \
    && curl -fsSL -o /tmp/Xray-linux-64.zip "https://github.com/XTLS/Xray-core/releases/latest/download/Xray-linux-64.zip" \
    && unzip -o /tmp/Xray-linux-64.zip -d /usr/local/bin/xray \
    && chmod +x /usr/local/bin/xray/xray \
    && rm /tmp/Xray-linux-64.zip

ENV PORT=8080

COPY nginx.conf /etc/nginx/nginx.conf
COPY entrypoint.sh /entrypoint.sh
COPY bot.py /opt/panel/bot.py
COPY index.html /usr/share/nginx/html/index.html

RUN chmod +x /entrypoint.sh

EXPOSE 8080

CMD ["/entrypoint.sh"]
