#!/usr/bin/env bash
# Lance le dashboard À LA MAIN (test) :   bash ~/drone_dashboard/run.sh
# En usage normal, le service "drone" le lance tout seul au branchement du Pi (install_service.sh).
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# clé Roboflow : fichier .env (créé par install_service.sh) ou variable déjà exportée
if [ -f "$DIR/.env" ]; then set -a; . "$DIR/.env"; set +a; fi
if [ -z "$ROBOFLOW_API_KEY" ]; then
  echo '❌ Clé manquante. Fais d abord :  export ROBOFLOW_API_KEY="ta_cle"   (ou bash install_service.sh)'
  exit 1
fi

# si le service tourne déjà, un 2e lancement échouerait (caméra + GPIO + port 5000 déjà pris)
if systemctl is-active --quiet drone.service 2>/dev/null; then
  echo "ℹ️  Le service 'drone' tourne déjà : le site est déjà en ligne."
  echo "   Pour lancer à la main : sudo systemctl stop drone   puis   bash run.sh"
  exit 1
fi

source ~/myenv/bin/activate
pkill -f rpicam 2>/dev/null
cd "$DIR"
python3 app.py
