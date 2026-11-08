#!/usr/bin/env bash
# Supprime le démarrage automatique (le dossier drone_dashboard reste intact).
sudo systemctl stop drone.service 2>/dev/null || true
sudo systemctl disable drone.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/drone.service
sudo systemctl daemon-reload
echo "✅ Service 'drone' supprimé. Lancement manuel : bash ~/drone_dashboard/run.sh"
