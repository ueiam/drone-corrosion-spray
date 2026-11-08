#!/usr/bin/env bash
# Installation complète sur le Raspberry Pi (à lancer UNE SEULE FOIS)
#   bash ~/drone_dashboard/setup_pi.sh
# Pas de reboot ici : si un redémarrage est nécessaire, coupe/rebranche l'alimentation du Pi à la main.
set -e

echo "▶ 1/3 Paquets système..."
sudo apt update
sudo apt install -y python3-venv python3-flask python3-requests python3-numpy \
                    python3-opencv python3-lgpio python3-picamera2

echo "▶ 2/3 Création de l'environnement virtuel ~/myenv (avec accès aux paquets système)..."
cd ~
rm -rf myenv
python3 -m venv --system-site-packages myenv

echo "▶ 3/3 Vérification..."
source ~/myenv/bin/activate
python3 -c "import flask, requests, numpy, cv2, lgpio; from picamera2 import Picamera2; print('✅ TOUT EST OK')"
echo
echo "Caméra :"
rpicam-hello --list-cameras || echo "⚠️  caméra non détectée (câble nappe ? Pi éteint pour le rebrancher)"
echo
echo "Terminé. Lance ensuite :  bash ~/drone_dashboard/run.sh"
