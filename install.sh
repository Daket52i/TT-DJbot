#!/usr/bin/env bash
# ============================================================================
#  Установщик djbot — радио-бот для TeamTalk 5
#  Форк: https://github.com/zolo-kirill/teamtalk-universal-plugin
# ============================================================================

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${GREEN}[+]${NC} $*"; }
warn() { echo -e "${YELLOW}[!]${NC} $*"; }
err()  { echo -e "${RED}[-]${NC} $*" >&2; }
info() { echo -e "${BLUE}[i]${NC} $*"; }

if [[ $EUID -ne 0 ]]; then
    err "Запустите от root: sudo ./install.sh"
    exit 1
fi

echo
echo "======================================================================"
echo "  Установка djbot — радио-бот для TeamTalk 5"
echo "======================================================================"
echo

# ---------- параметры TeamTalk ----------
read -rp "TeamTalk host (IP или домен) [127.0.0.1]: " TT_HOST
TT_HOST=${TT_HOST:-127.0.0.1}

read -rp "TCP порт [10333]: " TT_TCP
TT_TCP=${TT_TCP:-10333}

read -rp "UDP порт [10333]: " TT_UDP
TT_UDP=${TT_UDP:-10333}

read -rp "Использовать шифрование? (yes/no) [no]: " TT_ENC
TT_ENC=${TT_ENC:-no}

echo
echo "--- Учётная запись бота ---"
read -rp "Никнейм бота [!Океан музыки]: " BOT_NICK
BOT_NICK=${BOT_NICK:-!Океан музыки}

read -rp "Username [ocean_bot]: " BOT_USER
BOT_USER=${BOT_USER:-ocean_bot}

read -rsp "Пароль: " BOT_PASS; echo

read -rp "Канал (путь или ID) [/]: " BOT_CHANNEL
BOT_CHANNEL=${BOT_CHANNEL:-/}

read -rsp "Пароль канала (пусто = нет): " BOT_CHAN_PASS; echo

echo
echo "--- Яндекс.Музыка (опционально) ---"
read -rp "Токен Яндекс.Музыки (Enter = не использовать): " YM_TOKEN
YM_TOKEN=${YM_TOKEN:-}

echo
echo "--- YouTube куки (опционально) ---"
read -rp "Путь к файлу куки YouTube (Enter = пропустить): " YT_COOKIES
YT_COOKIES=${YT_COOKIES:-}

echo
echo "--- Telegram (опционально, Enter = пропустить) ---"
read -rp "Токен Telegram-бота []: " TG_TOKEN
TG_TOKEN=${TG_TOKEN:-}

read -rp "Ваш Telegram ID [0]: " TG_OWNER
TG_OWNER=${TG_OWNER:-0}

read -rp "Чат для уведомлений (ID) [0]: " TG_NOTIFY
TG_NOTIFY=${TG_NOTIFY:-0}

read -rp "Название сервера для Telegram [Мой сервер]: " TG_SERVER
TG_SERVER=${TG_SERVER:-Мой сервер}

echo
echo "--- Пути установки ---"
read -rp "Директория установки [/opt/djbot]: " INSTALL_DIR
INSTALL_DIR=${INSTALL_DIR:-/opt/djbot}

read -rp "Пользователь для запуска [ubuntu]: " RUN_USER
RUN_USER=${RUN_USER:-ubuntu}

echo
log "Начинаем установку в $INSTALL_DIR ..."

# ---------- пакеты ----------
log "Установка системных пакетов..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg git curl wget

# ---------- копирование файлов ----------
log "Копирование файлов бота..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$INSTALL_DIR"
cp -r "$SCRIPT_DIR"/bot.py "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR"/tt_register.py "$INSTALL_DIR/" 2>/dev/null || true
cp -r "$SCRIPT_DIR"/config_default.json "$INSTALL_DIR/"
cp -r "$SCRIPT_DIR"/radio "$INSTALL_DIR/" 2>/dev/null || true
cp -r "$SCRIPT_DIR"/geo "$INSTALL_DIR/" 2>/dev/null || true
cp -r "$SCRIPT_DIR"/sdk "$INSTALL_DIR/" 2>/dev/null || true

chown -R "$RUN_USER:$RUN_USER" "$INSTALL_DIR"

# ---------- venv ----------
log "Создание Python окружения..."
sudo -u "$RUN_USER" python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip setuptools wheel -q

# ---------- конфиг ----------
log "Генерация config.json..."
cat > "$INSTALL_DIR/config.json" <<EOF
{
  "version": 1,
  "server": {
    "host": "$TT_HOST",
    "tcp_port": $TT_TCP,
    "udp_port": $TT_UDP,
    "nickname": "$BOT_NICK",
    "username": "$BOT_USER",
    "password": "$BOT_PASS",
    "channel": "$BOT_CHANNEL",
    "channel_password": "$BOT_CHAN_PASS",
    "connect_timeout_sec": 20
  },
  "playback": {
    "default_volume": 10,
    "max_volume": 100,
    "pulse_sink": ""
  },
  "runtime": {
    "main_service": "yt",
    "send_to_channel": true,
    "startup_commands": [],
    "playlist_limit": 5000
  },
  "services": {
    "yt": { "cookiefile_path": "$YT_COOKIES" },
    "ym": { "token": "$YM_TOKEN" }
  },
  "telegram": {
    "token": "$TG_TOKEN",
    "owner_user_id": $TG_OWNER,
    "notify_chat_id": $TG_NOTIFY,
    "server_display_name": "$TG_SERVER",
    "ignore_usernames": [],
    "ignore_nicks": []
  },
  "guard": {
    "enabled": true,
    "guest_logins": ["гость", "guest"],
    "notify": true,
    "geo": { "enabled": false, "allow_countries": ["RU"] },
    "botnet_nick_check": true,
    "kick_empty_nick": false,
    "kick_empty_nick_all": false,
    "kick_empty_nick_ban": false,
    "max_nick_len": 40,
    "mat_check": true,
    "trusted": {
      "usernames": [],
      "nicknames": [],
      "ip_prefixes": ["127.0.0.", "10.", "192.168."]
    }
  }
}
EOF
chmod 600 "$INSTALL_DIR/config.json"
chown "$RUN_USER:$RUN_USER" "$INSTALL_DIR/config.json"

# ---------- systemd ----------
log "Установка systemd-юнита..."
cat > /etc/systemd/system/djbot.service <<EOF
[Unit]
Description=djbot — TeamTalk радио-бот
After=network.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$INSTALL_DIR/.venv/bin/python $INSTALL_DIR/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable djbot
systemctl restart djbot
sleep 5
systemctl is-active --quiet djbot && log "djbot: запущен!" || warn "djbot: проверьте логи: journalctl -u djbot"

echo
echo "======================================================================"
log "Установка завершена!"
echo "======================================================================"
echo
info "Управление:"
echo "  systemctl status djbot"
echo "  journalctl -u djbot -f"
echo "  systemctl restart djbot"
echo
info "Конфиг: $INSTALL_DIR/config.json"
info "Бот: $BOT_USER / $BOT_NICK → $BOT_CHANNEL"
echo
warn "Если хотите добавить токен Яндекс.Музыки — отредактируйте config.json"
warn "и перезапустите: systemctl restart djbot"
echo
