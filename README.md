
#  drone-corrosion-spray (AeroScan AI)

Détection de corrosion par vision et pulvérisation ciblée par servo, avec dashboard web temps réel. Module logiciel d'un drone d'inspection et de traitement anti-corrosion, tournant sur **Raspberry Pi 5**.

>  Ce dépôt contient uniquement la partie **détection de corrosion** et **contrôle du spray**. Le reste du drone (vol, contrôleur, etc.) n'est pas inclus.

---

##  Comment ça marche

```
Caméra (Picamera2)
      │
      ▼
Roboflow (modèle IA) ──► détecte et VALIDE la zone de corrosion
      │
      ▼
Filtre couleur (OpenCV, HSV) ──► affine la zone validée et extrait les contours
      │
      ▼
Décision ──► si corrosion confirmée : servo + spray (GPIO18)
      │
      ▼
Dashboard Flask ──► flux vidéo + statut en temps réel
```

1. La caméra capture l'image en continu.
2. **Roboflow** (modèle d'IA entraîné) détecte et valide la présence de corrosion.
3. **Après cette validation seulement**, le traitement par **filtre couleur** est appliqué pour délimiter précisément la zone (contours, masque).
4. Si la corrosion est confirmée, le **servo** actionne le spray.

---

##  Structure du projet

| Fichier | Rôle |
|---|---|
| `app.py` | Programme principal : serveur Flask, capture caméra, appel Roboflow, filtre couleur, flux vidéo, API |
| `spray.py` | Contrôle du servo et du spray (GPIO18) |
| `run.sh` | **Lance le script manuellement** (à utiliser pour tester) |
| `setup_pi.sh` | Installation des dépendances sur le Raspberry Pi |
| `install_service.sh` | Installe le projet comme **service** (démarrage automatique au boot) |
| `uninstall_service.sh` | Supprime le service |
| `fix_address.sh` | Correction de l'adresse réseau / IP du Pi |
| `servo_esp_replica.py` | Réplique du comportement du servo de l'ESP, pour tester le servo seul |

---

## 🔧 Matériel

- Raspberry Pi 5
- Caméra Raspberry Pi Module 3 (imx708), nappe CSI 22 broches
- Servo-moteur pour la buse de spray (signal sur **GPIO18**)
- Alimentation officielle **5V/5A**

---

## 🚀 Installation

```bash
git clone https://github.com/<ton-pseudo>/drone-corrosion-spray.git
cd drone-corrosion-spray
bash setup_pi.sh
```

Si tu fais l'installation à la main :

```bash
python3 -m venv --system-site-packages myenv
source myenv/bin/activate
pip install flask opencv-python numpy requests
```

> Picamera2 s'installe avec le système (`sudo apt install -y python3-picamera2`). C'est pour ça que l'environnement virtuel utilise `--system-site-packages`.

---

##  Configuration : TA clé API Roboflow (obligatoire)

Le projet utilise **Roboflow** pour valider la corrosion. **Tu dois utiliser ta propre clé API**, sinon la détection ne fonctionnera pas.

1. Crée un compte sur [roboflow.com](https://roboflow.com).
2. Récupère ta clé API (Settings → API Keys).
3. Remplace la clé et les informations du modèle dans `app.py` :

```python
ROBOFLOW_API_KEY = "<TA_CLE_API>"      # ← à changer
ROBOFLOW_MODEL   = "<ton-projet/version>"  # ← à changer si tu as ton propre modèle
```

 **Ne publie jamais ta vraie clé sur GitHub.** Utilise une variable d'environnement :

```bash
export ROBOFLOW_API_KEY="ta_cle"
```

et dans le code : `os.environ.get("ROBOFLOW_API_KEY")`.

---

##  Lancer le script (mode manuel)

```bash
cd drone-corrosion-spray
source myenv/bin/activate     # si (myenv) n'est pas déjà affiché
bash run.sh
```

Puis ouvre dans un navigateur : **`http://<IP_DU_PI>:5000`**

| Page | Contenu |
|---|---|
| `/` | Dashboard principal |
| `/overlay_feed` | Flux avec les contours de corrosion |
| `/mask_feed` | Masque du filtre couleur |
| `/api/status` | Statut en JSON |

## Arrêter le script

Un **seul** `Ctrl+C`, puis attendre quelques secondes (le servo se coupe proprement).

> Évite d'appuyer plusieurs fois sur `Ctrl+C` : ça peut bloquer la caméra.

---

##  Mode service (démarrage automatique)

```bash
sudo bash install_service.sh      # installe et active le service
sudo systemctl start drone        # démarrer
sudo systemctl stop drone         # arrêter / désactiver le script
sudo systemctl status drone       # voir l'état
journalctl -u drone -f            # logs en direct
sudo bash uninstall_service.sh    # supprimer le service
```

**Important :** si le service tourne, `bash run.sh` ne fonctionnera pas (la caméra est déjà utilisée). Fais d'abord `sudo systemctl stop drone`.

---

##  Dépannage

| Problème | Solution |
|---|---|
| `Failed to queue buffer` / caméra HS | Vérifier la nappe CSI, couper l'alim 30 s, rebrancher |
| `throttled` ≠ `0x0` | Alimentation insuffisante → utiliser du 5V/5A |
| Flux vidéo en retard | Passer en Ethernet, baisser la résolution/qualité JPEG |
| Pas de détection | Vérifier la clé API Roboflow et la connexion internet |

Test caméra seule :
```bash
rpicam-hello -t 3000 --nopreview
```

---

##  Personnaliser

- **Seuils du filtre couleur (HSV)** : dans `app.py`, pour s'adapter à ton type de rouille ou d'éclairage.
- **Seuil de confiance Roboflow** : pour être plus ou moins strict avant de déclencher le spray.
- **Angles du servo** : dans `spray.py`.

---

##  Licence

MIT
