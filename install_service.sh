#!/usr/bin/env bash
# Installe le démarrage AUTOMATIQUE du site : au branchement du Pi, le dashboard se lance tout seul
# (service systemd "drone"), redémarre tout seul en cas de plantage, et la page des navigateurs
# ouverts se recharge toute seule à chaque mise à jour.
#
#   bash ~/drone_dashboard/install_service.sh        (SANS sudo : il demande sudo tout seul)
set -e

if [ "$EUID" -eq 0 ]; then
  echo "❌ Lance-le SANS sudo :  bash install_service.sh"; exit 1
fi
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_NAME="$(id -un)"
PY="$HOME/myenv/bin/python3"

[ -x "$PY" ]          || { echo "❌ $PY introuvable : lance d'abord  bash $DIR/setup_pi.sh"; exit 1; }
[ -f "$DIR/app.py" ]  || { echo "❌ $DIR/app.py introuvable"; exit 1; }

# 1) clé Roboflow -> fichier .env (lisible par toi seul, jamais dans le code)
ENV_FILE="$DIR/.env"
if [ ! -s "$ENV_FILE" ]; then
  read -rsp "Colle ta clé API Roboflow (invisible) puis Entrée : " KEY; echo
  [ -n "$KEY" ] || { echo "❌ clé vide"; exit 1; }
  ( umask 077; printf 'ROBOFLOW_API_KEY=%s\n' "$KEY" > "$ENV_FILE" )
  echo "✅ clé enregistrée dans $ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# 2) un éventuel lancement manuel doit être arrêté (caméra / GPIO / port 5000)
pkill -TERM -f "python3 .*app.py" 2>/dev/null || true
sleep 3

# 3) service systemd
sudo tee /etc/systemd/system/drone.service >/dev/null <<UNIT
[Unit]
Description=AeroScan AI - dashboard d'inspection drone (Flask)

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DIR
EnvironmentFile=$ENV_FILE
Environment=PYTHONUNBUFFERED=1
ExecStartPre=-/usr/bin/pkill -f rpicam
ExecStart=$PY $DIR/app.py
Restart=always
RestartSec=5
# à l'arrêt : SIGTERM -> spray.py termine le cycle en cours (RETOUR) puis coupe le signal du servo
KillSignal=SIGTERM
TimeoutStopSec=25

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable drone.service
sudo systemctl restart drone.service
sleep 5
systemctl --no-pager --lines=12 status drone.service || true

IP="$(hostname -I | awk '{print $1}')"
echo
echo "════════════════════════════════════════════════════════════"
echo " ✅ Le site démarre maintenant tout seul à chaque allumage du Pi."
echo "    Adresse :  http://$IP:5000     ou     http://$(hostname).local:5000"
echo
echo " Commandes utiles :"
echo "    journalctl -u drone -f              voir les logs en direct"
echo "    sudo systemctl restart drone        appliquer une mise à jour du code"
echo "    sudo systemctl stop drone           arrêter le site (pour lancer run.sh à la main)"
echo "    bash $DIR/uninstall_service.sh      désinstaller le démarrage automatique"
echo
echo " Pour garder TOUJOURS la même adresse IP :  bash $DIR/fix_address.sh"
echo " (Pas de 'sudo reboot' à distance : pour tester, coupe et rebranche l'alimentation du Pi.)"
echo "════════════════════════════════════════════════════════════"
