# Pasteberth

**Pont entre le presse-papiers graphique d'un poste de travail et le
filesystem d'une machine distante où travaille un harness CLI/TUI
(OpenCode et similaires).**

Vous faites une capture d'écran sur votre poste, vous collez (Ctrl+V) dans
la zone du bon projet dans votre navigateur, et vous récupérez une référence
filesystem prête à coller dans OpenCode :

```
@/home/devint3/Depots/Pulse/.opencode-images/2026-08-25_01-22-31_a81c42.png
```

```
POSTE DE TRAVAIL                     MACHINE DU HARNESS
navigateur ── HTTPS ──▶ Pasteberth ──▶ .opencode-images/projet/
   ▲                        │
   └──── Copy référence ◀───┘
                │
                ▼
          terminal / OpenCode
```

Le navigateur n'a **jamais** besoin d'accéder au chemin retourné : il est
celui que voit OpenCode, sur la machine où tourne Pasteberth.

---

## Sommaire

1. [Fonctionnement](#fonctionnement)
2. [Installation](#installation)
3. [Configuration](#configuration)
4. [Mot de passe](#mot-de-passe)
5. [Lancement & service systemd](#lancement--service-systemd)
6. [HTTPS & reverse proxy](#https--reverse-proxy)
7. [API](#api)
8. [Sécurité](#sécurité)
9. [Tests](#tests)
10. [Limitations & V2](#limitations--v2)

---

## Fonctionnement

- **Zones** : la page affiche une carte par projet configuré (couleur,
  label, historique propre). On clique une zone pour la rendre active,
  puis Ctrl+V envoie l'image du clipboard vers cette zone.
  Glisser-déposer directement sur une carte fonctionne aussi.
- **Rétention circulaire par zone** (`retain = N`) : au-delà de N images,
  les plus anciennes sont supprimées — uniquement des fichiers créés par
  Pasteberth (preuve de paternité par sidecar JSON).
- **Référence exacte** : le serveur construit et retourne la référence ;
  le frontend la copie telle quelle (`navigator.clipboard.writeText`,
  repli `execCommand`), sans jamais reconstruire le chemin côté client.
- **Page permanente** : prévue pour rester ouverte des heures ; pas de
  Blob URL accumulée (miniatures servies par le serveur), re-rendu par
  zone, resynchronisation légère toutes les 45 s + au retour sur l'onglet.

## Installation

Prérequis : **Python ≥ 3.11**, aucune dépendance tierce (bibliothèque
standard uniquement). Aucun root nécessaire.

```sh
./install.sh        # symlink ~/.local/bin/pasteberth -> run.sh
                    # + config exemple dans ~/.config/pasteberth/config.toml
                    # + unité systemd --user copiée
```

Sans install.sh :

```sh
~/.local/bin/pasteberth: ./run.sh serve   # ou python3 -m pasteberth serve
cp config.example.toml ~/.config/pasteberth/config.toml
```

## Configuration

Fichier : `~/.config/pasteberth/config.toml`
(surcharge : `--config PATH` ou `$PASTEBERTH_CONFIG`). Voir
[`config.example.toml`](config.example.toml) commenté.

| Clé | Défaut | Rôle |
|---|---|---|
| `listen_address` | `"127.0.0.1"` | écoute ; non-loopback exige l'auth |
| `port` | `8765` | port TCP |
| `max_upload_size` | `"20MB"` | plafond par upload (KB/MiB/GB acceptés) |
| `trusted_proxies` | loopback | seuls ces pairs peuvent poser `X-Forwarded-*` |
| `allow_unauthenticated_remote` | `false` | déverrouillage explicite (déconseillé) |
| `log_level` | `"INFO"` | DEBUG/INFO/WARNING/ERROR |
| `[auth] enabled` | `false` | protection par mot de passe |
| `[auth] session_ttl_hours` | `72` | durée des sessions serveur |
| `[[zones]] …` | — | `id`, `label`, `type=local`, `directory`, `retain`, `reference_prefix`, `color` (#RRGGBB), `create_directory` |

`directory` est un chemin **absolu vu par le serveur** — c'est là
qu'OpenCode lit les images, pas votre navigateur.

## Mot de passe

```sh
pasteberth passwd            # demande + confirmation, hash scrypt salé
                             # écrit dans ~/.config/pasteberth/passwd (0600)
```

Le mot de passe n'est jamais stocké en clair ni écrit dans config.toml ;
le hash est vérifié avec `hashlib.scrypt` + comparaison en temps constant.
Un changement est effectif immédiatement (rechargé à chaque tentative),
sans redémarrer le service.

## Lancement & service systemd

```sh
pasteberth serve                          # premier plan
systemctl --user enable --now pasteberth.service   # service permanent
journalctl --user -u pasteberth -f        # logs
```

L'unité fournie (`deploy/pasteberth.service`) ne nécessite aucun root.
Un refus de démarrage protège contre l'exposition accidentelle :
**écoute non-loopback + auth désactivée = arrêt avec message explicite**
(déroutable uniquement via `allow_unauthenticated_remote = true`).

## HTTPS & reverse proxy

Pasteberth transporte mot de passe, sessions et chemins privés :
**tout accès réseau non fiable doit passer par HTTPS.** En V1 le service
ne fait pas TLS lui-même ; placez-le derrière un reverse proxy.

### Caddy (recommandé)

```caddy
pasteberth.example.internal {
    reverse_proxy 127.0.0.1:8765
}
```

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name pasteberth.example.internal;
    # ssl_certificate …; ssl_certificate_key …;
    location / {
        proxy_pass http://127.0.0.1:8765;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $remote_addr;
        client_max_body_size 25m;
    }
}
```

Dans les deux cas : écoutez Pasteberth sur `127.0.0.1`, et laissez
`trusted_proxies` contenir uniquement l'IP du proxy. Les en-têtes
`X-Forwarded-Proto/For` provenant d'un pair non listé sont **ignorés**
(un client Internet ne peut pas forcer un cookie `Secure` ou usurper
une IP auprès du limiteur).

## API

Same-origin uniquement (aucun CORS en V1 ; cookies de session).

| Méthode | Chemin | Rôle |
|---|---|---|
| GET | `/api/health` | sonde (public) |
| GET | `/api/zones` | zones + compteurs |
| GET | `/api/zones/{id}/images` | historique, plus récent d'abord |
| POST | `/api/zones/{id}/images` | upload (multipart champ `image`, ou corps brut `image/*` / `application/octet-stream`) |
| GET | `/previews/{id}/{fichier}` | miniature (protégée) |

Exemple :

```sh
curl -b cookies.txt -F image=@capture.png \
     https://pasteberth.example.internal/api/zones/pulse/images
```

```json
{
  "id": "2026-08-25_01-22-31_a81c42.png",
  "filename": "2026-08-25_01-22-31_a81c42.png",
  "created_at": "2026-08-24T23:22:31.412000+00:00",
  "width": 1920, "height": 1080, "size": 9283, "format": "png",
  "preview_url": "/previews/pulse/2026-08-25_01-22-31_a81c42.png",
  "reference": "@/home/devint3/Depots/Pulse/.opencode-images/2026-08-25_01-22-31_a81c42.png"
}
```

Formats : PNG, JPEG, WebP — déterminés par le **contenu** (magic bytes +
structure), jamais par le MIME déclaré. Refus : vide, trop grand, format
inconnu, image corrompue. Les noms sont générés côté serveur
(`AAAA-MM-JJ_HH-MM-SS_<6 hex>.ext`, création `O_EXCL` : zéro écrasement).

## Sécurité

- Mots de passe : scrypt salé (N=16384), comparaison temps constant,
  fichier 0600 hors dépôt ; temporisation + verrouillage progressif par IP
  (honorant XFF seulement via proxy de confiance).
- Sessions côté serveur, révocables (logout effectif), token 256 bits,
  cookie `HttpOnly; SameSite=Lax` + `Secure` dès que le schéma effectif
  est HTTPS.
- CSRF : SameSite=Lax + toute requête non sûre avec `Origin`/`Referer`
  doit correspondre à l'hôte servi (403 sinon). Pas d'`Access-Control-*`.
- CSP stricte sans inline, `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store` sur l'UI/API.
- Previews et API exigent la session ; un nom de fichier ne peut pas
  traverser (`[A-Za-z0-9._-]` strict + appartenance à l'historique requis).
- Seuls les fichiers dotés d'un sidecar Pasteberth peuvent être lus ou
  supprimés ; vos fichiers personnels dans les répertoires cibles ne sont
  jamais touchés.
- Rétention sous verrou par zone : ordre déterministe, uploads concurrents
  sûrs (tests dédiés).

## Tests

```sh
python3 -m unittest discover -s tests -v
```

139 tests : validation images (PNG/JPEG/WebP, corruption, spoofing),
configuration & politique de démarrage, stockage/rétention/ownership,
auth/sessions/anti-bruteforce, parser multipart, intégration HTTP complète
(auth, CSRF/Origin, proxys, en-têtes, fuite de secret), concurrence
(uploads parallèles même/multi zones, lecteurs pendant écritures),
CLI (passwd, refus de configuration dangereuse), contrats frontend.

## Limitations & V2

- Destination unique `local` (relative au serveur). L'abstraction
  `Destination` est prête pour une `SshDestination` (SFTP, credentials
  restant côté serveur).
- Pas d'extension navigateur : l'API est utilisable telle quelle, mais le
  CORS dédié sera ajouté explicitement le moment venu.
- Sessions en mémoire : un redémarrage déconnecte (volontaire, simple) ;
  suppression manuelle d'une image hors rétention à prévoir en UI.
- Mono-utilisateur par conception ; TLS délégué au reverse proxy.
