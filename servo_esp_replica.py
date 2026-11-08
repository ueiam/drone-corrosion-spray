#!/usr/bin/env python3
"""
servo_esp_replica.py — COPIE EXACTE du code Arduino/ESP, pour Raspberry Pi 5

    monServo.write(90);  delay(1000);           # arrêt au démarrage
    monServo.write(120); delay(280);            # ALLER
    monServo.write(90);                         # arrêt
    delay(5000);                                # PAUSE 5 s
    monServo.write(60);  delay(280);            # RETOUR
    monServo.write(90);                         # arrêt définitif

Comme sur l'ESP : le signal n'est JAMAIS coupé, le servo reçoit en permanence
la dernière valeur écrite (write(90) = neutre = arrêt).
write(angle) est converti en impulsion exactement comme la bibliothèque Servo.h
(0..180 -> 544..2400 µs) : write(120)=1781 µs, write(90)=1472 µs, write(60)=1162 µs.

Usage :
    source ~/myenv/bin/activate
    python3 servo_esp_replica.py

⚠️ Bras en position de REPOS au lancement. Pas en même temps que app.py (même GPIO).
   Ctrl+C : si le bouton est enfoncé, le script fait le RETOUR avant de quitter.
"""

import time

import lgpio

# ─── RÉGLAGES ────────────────────────────────────────────
PIN_SERVO          = 18      # GPIO18 (pin physique 12)   [ESP : broche 9]
ANGLE_DEGRES       = 130     # course de l'appui : 0 -> 130° puis retour à 0 (comme le code ESP)
DELAI_ROTATION     = max(20, round(280 * ANGLE_DEGRES / 130))   # ms : 130° = 280 ms au calibrage ESP -> 50° = 108 ms
PAUSE_MAINTIEN     = 5000    # ms  (delay(5000))
BOUCLE             = True    # True = en boucle ; False = une seule fois comme l'ESP
PAUSE_ENTRE_CYCLES = 3000    # ms entre deux cycles (servo au neutre write(90))
# ─────────────────────────────────────────────────────────

MIN_PULSE_US, MAX_PULSE_US = 544, 2400     # valeurs par défaut de Servo.h


class Servo:
    """Équivalent de la classe Servo d'Arduino (attach / write / detach)."""

    def __init__(self):
        self.h = None
        self.pin = None

    def attach(self, pin):
        last = None
        for chip in (4, 0):                 # gpiochip4 (RP1) sur Pi 5, sinon gpiochip0
            hh = None
            try:
                hh = lgpio.gpiochip_open(chip)
                lgpio.gpio_claim_output(hh, pin, 0)
                self.h, self.pin = hh, pin
                return
            except Exception as e:
                last = e
                if hh is not None:
                    try:
                        lgpio.gpiochip_close(hh)
                    except Exception:
                        pass
        raise SystemExit(f"❌ Impossible d'ouvrir GPIO{pin} : {last}\n"
                         "   (app.py ou un autre script tourne déjà ? python3-lgpio installé ?)")

    def write(self, angle):
        """Même conversion que Servo.write() : map(angle, 0, 180, 544, 2400)."""
        angle = max(0, min(180, int(angle)))
        us = (angle * (MAX_PULSE_US - MIN_PULSE_US)) // 180 + MIN_PULSE_US
        lgpio.tx_servo(self.h, self.pin, us, 50)    # répété toutes les 20 ms jusqu'au prochain write

    def detach(self):
        if self.h is None:
            return
        try:
            lgpio.tx_servo(self.h, self.pin, 0)     # coupe le signal
            lgpio.gpio_write(self.h, self.pin, 0)
            lgpio.gpiochip_close(self.h)
        finally:
            self.h = None


def delay(ms):
    time.sleep(ms / 1000.0)


monServo = Servo()
au_bout = False          # True entre l'ALLER et le RETOUR (bouton enfoncé)


def setup():
    monServo.attach(PIN_SERVO)
    monServo.write(90)       # Arrêt au démarrage
    delay(1000)              # Pause d'une seconde avant le départ


def cycle():
    global au_bout
    # 1. ALLER
    print(f"▶ ALLER   write(120) {DELAI_ROTATION} ms  (0 -> {ANGLE_DEGRES}°)")
    monServo.write(120)
    delay(DELAI_ROTATION)
    monServo.write(90)       # Arrêt du moteur
    au_bout = True

    # 2. PAUSE
    print(f"⏸ PAUSE   {PAUSE_MAINTIEN/1000:.0f} s")
    delay(PAUSE_MAINTIEN)

    # 3. RETOUR
    print(f"◀ RETOUR  write(60) {DELAI_ROTATION} ms  ({ANGLE_DEGRES}° -> 0)")
    monServo.write(60)
    delay(DELAI_ROTATION)
    monServo.write(90)       # Arrêt définitif
    au_bout = False
    print("✔ cycle terminé\n")


def main():
    print(__doc__)
    setup()
    n = 0
    try:
        while True:
            n += 1
            print(f"── Cycle {n} ──")
            cycle()
            if not BOUCLE:
                break
            delay(PAUSE_ENTRE_CYCLES)
    except KeyboardInterrupt:
        print("\n🛑 Arrêt demandé.")
    finally:
        if au_bout:
            print("   Bouton enfoncé -> RETOUR avant de quitter")
            monServo.write(60)
            delay(DELAI_ROTATION)
            monServo.write(90)
            delay(200)
        monServo.detach()
        print("Servo coupé. Terminé.")


if __name__ == "__main__":
    main()
