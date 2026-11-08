#!/usr/bin/env bash
# Fige l'adresse IP ACTUELLE du Pi (Wi-Fi) pour qu'elle ne change plus : le site reste toujours
# sur la même adresse. À lancer une fois, connecté au bon Wi-Fi (hotspot du PC) :
#   bash ~/drone_dashboard/fix_address.sh
# Pas de redémarrage à distance : la nouvelle config s'applique à la prochaine connexion Wi-Fi
# (donc au prochain rebranchement de l'alimentation du Pi). L'adresse reste la même qu'aujourd'hui.
set -e
command -v nmcli >/dev/null || { echo "❌ nmcli introuvable (NetworkManager absent)"; exit 1; }

CON="$(nmcli -t -f NAME,TYPE connection show --active | awk -F: '$2=="802-11-wireless"{print $1; exit}')"
[ -n "$CON" ] || { echo "❌ Aucune connexion Wi-Fi active"; exit 1; }
IP="$(ip -4 -o addr show wlan0 | awk '{print $4}' | head -n1)"
GW="$(ip route | awk '/^default/ && /wlan0/ {print $3; exit}')"
[ -n "$IP" ] && [ -n "$GW" ] || { echo "❌ IP ($IP) ou passerelle ($GW) introuvable"; exit 1; }

echo "Wi-Fi : $CON | adresse figée : $IP | passerelle/DNS : $GW"
sudo nmcli connection modify "$CON" ipv4.method manual ipv4.addresses "$IP" ipv4.gateway "$GW" ipv4.dns "$GW"
echo "✅ Adresse IP figée. Active au prochain rebranchement du Pi."
echo "   (Pour annuler : sudo nmcli connection modify \"$CON\" ipv4.method auto ipv4.addresses \"\" ipv4.gateway \"\")"
