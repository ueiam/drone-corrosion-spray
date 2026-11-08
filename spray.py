#!/usr/bin/env python3
"""
spray.py — Servo qui appuie sur la bombe anti-corrosion (Raspberry Pi 5)

RÈGLE : le servo ne fait AUCUN mouvement sans détection de corrosion confirmée.
    - rien détecté        -> signal COUPÉ (0 V sur le fil) : le servo ne reçoit rien, il ne bouge pas
    - corrosion confirmée -> UN SEUL cycle complet par zone (jamais interrompu), puis plus rien
      tant que la zone n'est pas redevenue saine pendant REARM_SAIN_S secondes :

        (neutre write(90) 300 ms : le servo « se réveille », comme setup() de l'ESP)
        ALLER   write(120) pendant DELAI_ROTATION, puis write(90)   (appui : 0 -> 50°)
        PAUSE   PAUSE_MAINTIEN au neutre write(90)                  (le bouton reste enfoncé)
        RETOUR  write(60)  pendant DELAI_ROTATION, puis write(90)   (relâche : 50° -> 0)
        (neutre 300 ms, puis signal COUPÉ jusqu'à la prochaine détection)

Pendant le cycle, le servo reçoit EXACTEMENT les mêmes impulsions que dans servo_esp_replica.py.
Seul le repos (entre deux cycles) est silencieux : un neutre envoyé en permanence par le Pi
(PWM logiciel instable quand la caméra charge le processeur) fait avancer/reculer le servo
par petits pas. Sans signal, il reste immobile.

Arrêt propre : Ctrl+C, kill (SIGTERM) et fermeture du terminal (SIGHUP) terminent
d'abord le cycle en cours (RETOUR inclus), puis coupent le signal et libèrent la ligne.

write(angle) = même conversion que Servo.write() d'Arduino (0..180 -> 544..2400 µs) :
120 = 1781 µs, 90 = 1472 µs, 60 = 1162 µs.

⚠️ Bras en position de REPOS au lancement. Ne pas lancer avec servo_esp_replica.py en même temps.
⚠️ MATÉRIEL : résistance de 10 kΩ entre le fil signal du servo et GND (voir explications),
   et AUCUN condensateur sur le fil signal (les condensateurs vont sur + et − du servo).
"""

import signal
import threading
import time

import lgpio

# ─── RÉGLAGES ────────────────────────────────────────────
PIN_SERVO            = 18      # GPIO18 (pin physique 12)
# Course de l'appui : 0 -> ANGLE_DEGRES puis retour à 0.
# Calibrage du code ESP : 130° parcourus en 280 ms (write(120) / write(60)).
# Servo à rotation continue : l'angle = vitesse x temps -> durée = 280 ms x ANGLE / 130.
ANGLE_DEGRES         = 130     # course de l'appui : 0 -> 130° puis retour à 0 (mouvement d'origine de l'ESP)
REF_DEGRES           = 130     # référence de calibrage ESP
REF_MS               = 280     # durée de la référence (ms) -> ajuste-la si la course réelle est trop courte/longue
DELAI_ROTATION       = max(20, round(REF_MS * ANGLE_DEGRES / REF_DEGRES))   # ms (130° -> 280 ms)

PAUSE_MAINTIEN       = 1000    # ms  : bouton enfoncé (neutre maintenu)
PAUSE_ENTRE_CYCLES   = 3000    # ms  : repos avant d'accepter une nouvelle détection
NEUTRE_AVANT_MS      = 300     # ms  : write(90) avant l'ALLER (le servo se réveille, comme setup() de l'ESP)
NEUTRE_APRES_MS      = 300     # ms  : write(90) après le RETOUR, puis signal coupé
REARM_SAIN_S         = 3.0     # s   : la zone doit être SAINE pendant ce temps avant d'accepter un nouveau cycle
                               #       (1 seul cycle tant que la rouille reste devant la caméra)

SPRAY_ARMED_AT_START = True    # True = actif dès le lancement ; False = il faut cliquer "Arm spray"
# ─────────────────────────────────────────────────────────

MIN_PULSE_US, MAX_PULSE_US = 544, 2400     # valeurs par défaut de Servo.h


class Servo:
    """Équivalent de la classe Servo d'Arduino (attach / write / detach) + stop()."""

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
        raise RuntimeError(f"Impossible d'ouvrir GPIO{pin} : {last}")

    def write(self, angle):
        """Même conversion que Servo.write() : map(angle, 0, 180, 544, 2400)."""
        angle = max(0, min(180, int(angle)))
        us = (angle * (MAX_PULSE_US - MIN_PULSE_US)) // 180 + MIN_PULSE_US
        lgpio.tx_servo(self.h, self.pin, us, 50)    # répété toutes les 20 ms jusqu'au prochain write/stop

    def stop(self):
        """Coupe les impulsions ET force la ligne à 0 V : le servo ne reçoit plus rien."""
        if self.h is None:
            return
        try:
            lgpio.tx_servo(self.h, self.pin, 0)
            lgpio.gpio_write(self.h, self.pin, 0)
        except Exception:
            pass

    def detach(self):
        if self.h is None:
            return
        try:
            self.stop()
            lgpio.gpiochip_close(self.h)
        except Exception:
            pass
        finally:
            self.h = None


def delay(ms):
    time.sleep(ms / 1000.0)


class SprayController:
    def __init__(self):
        self.lock = threading.Lock()
        self.servo = Servo()
        self.available = False
        self.error = None
        self.closed = False
        try:
            self.servo.attach(PIN_SERVO)
            self.servo.stop()            # ligne à 0 V dès le démarrage : aucun signal, aucun mouvement
            self.available = True
            print(f"[SPRAY] Servo prêt sur GPIO{PIN_SERVO} — signal coupé au repos")
        except Exception as e:
            self.error = str(e)
            print(f"[SPRAY] ⚠️  Servo indisponible ({e}) — mode SIMULATION (aucun mouvement)")

        self.state = "rest"              # rest | spraying | cooldown
        self.armed = SPRAY_ARMED_AT_START
        self.t_press = 0.0
        self.t_cooldown_end = 0.0
        self.presses = 0
        self.latched = False             # True = un cycle a déjà eu lieu pour cette zone
        self.t_clear_since = None        # depuis quand la zone est saine (pour le ré-armement)
        self.thread = None
        self._install_signal_handlers()
        print(f"[SPRAY] {'ARMÉ' if self.armed else 'DÉSARMÉ (clique Arm spray)'}")

    # ─── arrêt propre sur SIGTERM / SIGHUP (Ctrl+C est géré par Python + atexit) ───
    def _install_signal_handlers(self):
        def handler(signum, frame):
            print(f"[SPRAY] Signal {signum} reçu : arrêt propre du servo...")
            self.shutdown()
            raise SystemExit(0)

        for name in ("SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):    # pas dans le thread principal
                pass

    def _cycle(self):
        au_bout = False                      # True entre l'ALLER et le RETOUR (bouton enfoncé)
        try:
            if self.available:
                self.servo.write(90)         # neutre : le servo se réveille (comme setup() de l'ESP)
                delay(NEUTRE_AVANT_MS)

                print(f"▶ [SPRAY] ALLER   write(120) {DELAI_ROTATION} ms  (0 -> {ANGLE_DEGRES}°)")
                self.servo.write(120)
                delay(DELAI_ROTATION)
                self.servo.write(90)         # Arrêt du moteur (neutre maintenu)
                au_bout = True

                print(f"⏸ [SPRAY] PAUSE   {PAUSE_MAINTIEN / 1000:.1f} s")
                delay(PAUSE_MAINTIEN)

                print(f"◀ [SPRAY] RETOUR  write(60) {DELAI_ROTATION} ms  ({ANGLE_DEGRES}° -> 0)")
                self.servo.write(60)
                delay(DELAI_ROTATION)
                self.servo.write(90)         # Arrêt
                au_bout = False
                delay(NEUTRE_APRES_MS)
            else:
                delay(PAUSE_MAINTIEN)        # simulation : même durée, sans mouvement
            print("✔ [SPRAY] cycle terminé")
        except Exception as e:
            self.error = str(e)
            print(f"[SPRAY] ⚠️  erreur pendant le cycle : {e}")
        finally:
            if au_bout and self.available:   # erreur entre ALLER et RETOUR : on tente quand même le RETOUR
                try:
                    self.servo.write(60)
                    delay(DELAI_ROTATION)
                    self.servo.write(90)
                    delay(NEUTRE_APRES_MS)
                except Exception:
                    pass
            self.servo.stop()                # repos : plus aucun signal, ligne à 0 V
            with self.lock:
                self.state = "cooldown"
                self.t_cooldown_end = time.time() + PAUSE_ENTRE_CYCLES / 1000.0
            delay(PAUSE_ENTRE_CYCLES)
            with self.lock:
                self.state = "rest"

    def _start_cycle(self):
        """Appelé avec self.lock pris, uniquement si state == 'rest'."""
        self.state = "spraying"
        self.t_press = time.time()
        self.presses += 1
        self.thread = threading.Thread(target=self._cycle, daemon=False)
        self.thread.start()

    # ─── API publique (inchangée pour app.py) ───
    def update(self, status):
        """status : 'corrosion' | 'probable' | 'healthy' (appelé à chaque analyse).

        UN SEUL cycle par zone : après un cycle, plus aucun mouvement tant que la zone n'est pas
        redevenue SAINE pendant REARM_SAIN_S secondes (la rouille qui reste visible ne relance rien).
        """
        with self.lock:
            if self.closed:
                return
            now = time.time()
            if self.latched:
                if status == "healthy":
                    if self.t_clear_since is None:
                        self.t_clear_since = now
                    elif now - self.t_clear_since >= REARM_SAIN_S:
                        self.latched = False
                        self.t_clear_since = None
                        print(f"🔓 [SPRAY] zone saine depuis {REARM_SAIN_S:.0f} s : prêt pour une nouvelle détection")
                else:
                    self.t_clear_since = None
                return
            if self.armed and self.state == "rest" and status == "corrosion":
                print("💨 [SPRAY] corrosion détectée → 1 cycle complet (puis attente que la zone soit saine)")
                self.latched = True
                self.t_clear_since = None
                self._start_cycle()

    def force_release(self):
        """Ignoré volontairement : un cycle commencé va toujours jusqu'au RETOUR."""
        return

    def set_armed(self, on):
        with self.lock:
            self.armed = bool(on)
            print(f"🔐 [SPRAY] {'ARMÉ' if self.armed else 'DÉSARMÉ'} (un cycle en cours se termine)")

    def test(self):
        """Lance un cycle complet manuel (si rien n'est en cours)."""
        with self.lock:
            if not self.closed and self.state == "rest":
                print("🧪 [SPRAY] TEST → cycle complet")
                self._start_cycle()

    def snapshot(self):
        with self.lock:
            now = time.time()
            sp = self.state == "spraying"
            cd = self.state == "cooldown"
            return {
                "available": self.available,
                "armed": self.armed,
                "state": self.state,
                "held": round(now - self.t_press, 1) if sp else 0.0,
                "cooldown_left": round(max(0.0, self.t_cooldown_end - now), 1) if cd else 0.0,
                "presses": self.presses,
                "latched": self.latched,
                "min_hold": PAUSE_MAINTIEN / 1000.0,
                "max_hold": PAUSE_MAINTIEN / 1000.0,
                "error": self.error,
            }

    def shutdown(self):
        """Arrêt : attend la fin du cycle en cours (RETOUR inclus), puis coupe le signal. Idempotent."""
        with self.lock:
            if self.closed:
                return
            self.closed = True               # plus aucun nouveau cycle
        t = self.thread
        if t is not None and t.is_alive():
            print("[SPRAY] Arrêt : j'attends la fin du cycle (RETOUR) avant de couper...")
            t.join(timeout=(2 * DELAI_ROTATION + NEUTRE_AVANT_MS + NEUTRE_APRES_MS + PAUSE_MAINTIEN + PAUSE_ENTRE_CYCLES) / 1000.0 + 2)
        with self.lock:
            if self.available:
                self.servo.stop()
                delay(100)
                self.servo.detach()
                self.available = False
        print("[SPRAY] Servo coupé (signal à 0).")
