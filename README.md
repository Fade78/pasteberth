# Pasteberth

**Pont entre le presse-papiers graphique d'un poste de travail et le
filesystem d'une machine distante où travaille un harness CLI/TUI
(OpenCode et similaires).**

Vous faites une capture d'écran sur votre poste, vous collez (Ctrl+V) dans
la zone du bon projet dans votre navigateur, et vous récupérez une référence
filesystem prête à coller dans OpenCode :

```
@/chemin/du/depot/storage/default/2026-08-25_01-22-31_a81c42.png
```

```
POSTE DE TRAVAIL                     MACHINE DU HARNESS
navigateur ── HTTPS ──▶ Pasteberth ──▶ storage/default/
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
  Pasteberth dans un répertoire privé (preuve de paternité par sidecar JSON).
- **Référence exacte** : le serveur construit et retourne la référence ;
  le frontend la copie telle quelle (`navigator.clipboard.writeText`,
  repli `execCommand`), sans jamais reconstruire le chemin côté client.
- **Page permanente** : prévue pour rester ouverte des heures ; pas de
  Blob URL accumulée (miniatures servies par le serveur), re-rendu par
  zone, resynchronisation légère toutes les 45 s + au retour sur l'onglet.

## Installation

Prérequis : **Python ≥ 3.11**, aucune dépendance tierce (bibliothèque
standard uniquement). Le dépôt est directement l'installation ; aucun script
d'installation ni root ne sont nécessaires.

```sh
git clone https://glb.didierb.name/didier/pasteberth.git
cd pasteberth
./bin/pasteberth audit
./bin/pasteberth --generate-config
./bin/pasteberth passwd
./bin/pasteberth
```

Pour utiliser la commande depuis n'importe quel répertoire, ajoutez
`bin/` au `PATH` ou utilisez directement `./bin/pasteberth`.

```sh
export PATH="$PWD/bin:$PATH"
pasteberth
```

## Configuration

Après génération, le fichier local est `config.toml` à la racine du dépôt et
reste ignoré par Git. Une configuration explicite peut aussi être fournie par
`--config PATH` ou `$PASTEBERTH_CONFIG`. Une ancienne configuration XDG dans
`~/.config/pasteberth/config.toml` reste reconnue. Voir
[`config.example.toml`](config.example.toml) commenté.

Sans `config.toml`, `pasteberth` démarre volontairement en mode minimal,
uniquement sur loopback, avec le stockage `<depot>/storage/default` et sans
authentification. Un avertissement est affiché à chaque démarrage. Le même
avertissement apparaît si une configuration modifiée continue de cibler ce
stockage par défaut. Ce mode sert au premier essai local, pas à une exposition
via reverse proxy.

`pasteberth --generate-config` génère une configuration sécurisée avec
authentification activée. Modifiez ensuite manuellement `config.toml` selon
les zones et chemins souhaités.

| Clé | Défaut | Rôle |
|---|---|---|
| `listen_address` | `"127.0.0.1"` | écoute ; non-loopback exige HTTPS explicite |
| `port` | `8765` | port TCP |
| `max_upload_size` | `"20MiB"` | plafond par upload (20 MiB par défaut, 50 MiB maximum) |
| `max_image_pixels` | `25000000` | budget de décodage (25 MP par défaut, 50 MP maximum) |
| `trusted_proxies` | loopback | seuls ces pairs peuvent poser `X-Forwarded-*` |
| `allow_unauthenticated_local` | `false` | opt-in explicite pour le mode anonyme loopback/proxy |
| `allow_unauthenticated_remote` | `false` | déverrouillage explicite (déconseillé) |
| `allow_insecure_http_remote` | `false` | opt-in séparé pour HTTP non-loopback (réseau privé uniquement) |
| `log_level` | `"INFO"` | DEBUG/INFO/WARNING/ERROR |
| `[tls] enabled` | `false` | termine TLS directement avec `certificate` et `private_key` |
| `[auth] enabled` | `true` | protection par mot de passe |
| `[auth] session_ttl_hours` | `72` | durée des sessions serveur |
| `[auth] password_file` | à côté de `config.toml` | chemin absolu du hash `passwd` (fichier régulier 0600) |
| `[[zones]] …` | `default` | `id`, `label`, `type=local`, `directory`, `retain`, `reference_prefix`, `color` (#RRGGBB), `create_directory`, `min_free_percent` |

`directory` est un chemin **absolu vu par le serveur** — c'est là
qu'OpenCode lit les images, pas votre navigateur.

Le stockage intégré par défaut est `<racine-du-dépôt>/storage/default`. Le
répertoire `storage/` est ignoré par Git, mais il faut le sauvegarder séparément
si les images ont de la valeur. Un chemin externe peut être indiqué
manuellement dans `config.toml`.

Les zones doivent être des répertoires cibles distincts et accessibles en écriture.
Le mode privé `0700` est recommandé ; un mode plus ouvert produit un avertissement
à l'audit mais n'empêche pas le démarrage, ce qui permet un partage contrôlé entre
plusieurs utilisateurs. Chaque zone refuse un nouvel upload si l'espace libre prévu après écriture passerait
sous `min_free_percent` (défaut `2.0`). La mesure porte sur le filesystem du
répertoire, pas sur le seul dossier ; plusieurs zones peuvent donc partager un
filesystem, mais elles partagent alors aussi sa réserve d'espace.

Les images sont limitées à `16 384 × 16 384` pixels et `25 MP` par défaut,
ce qui couvre les écrans 4K à 6K usuels. Les images 8K dépassant `25 MP`
nécessitent une extension explicite du budget.

## Mot de passe

```sh
pasteberth passwd            # demande + confirmation, hash scrypt salé
                             # écrit dans password_file ou à côté de config.toml (0600)
```

Le mot de passe n'est jamais stocké en clair ni écrit dans config.toml ;
le hash est vérifié avec `hashlib.scrypt` + comparaison en temps constant.
Un changement est effectif immédiatement (rechargé à chaque tentative),
sans redémarrer le service, et invalide les sessions existantes.
Le serveur refuse de démarrer si l'authentification est activée sans fichier
`passwd` lisible et valide.

## Lancement & service systemd

```sh
pasteberth                         # premier plan, stockage par défaut si besoin
pasteberth audit                   # vérification sans modification
systemctl --user enable --now pasteberth.service   # optionnel
journalctl --user -u pasteberth -f                 # logs
```

L'unité fournie (`deploy/pasteberth.service`) est optionnelle et ne nécessite
aucun root. Adaptez son `ExecStart` au chemin réel du dépôt avant activation.
Un refus de démarrage protège contre l'exposition accidentelle :
**auth désactivée sans opt-in explicite = arrêt avec message explicite**
(`allow_unauthenticated_local` ou `allow_unauthenticated_remote` selon le cas).
Une écoute HTTP non-loopback exige également
`allow_insecure_http_remote = true`; la configuration recommandée reste un
backend loopback derrière un reverse proxy HTTPS.

Pour que le service utilisateur survive à la dernière déconnexion et démarre
au boot, activez le linger pour le compte concerné :

```sh
loginctl enable-linger "$USER"
```

Cette option maintient un gestionnaire systemd utilisateur actif même sans
session interactive ; activez-la seulement si cette persistance est souhaitée.

## HTTPS & reverse proxy

Pasteberth transporte mot de passe, sessions et chemins privés :
**tout accès réseau non fiable doit passer par HTTPS.** Le reverse proxy reste
recommandé, mais le serveur peut aussi terminer TLS directement :

```toml
[tls]
enabled = true
certificate = "/chemin/absolu/cert.pem"
private_key = "/chemin/absolu/key.pem"
```

Avec une écoute non-loopback, activez TLS ou utilisez un reverse proxy HTTPS.
L'option `allow_insecure_http_remote = true` ne doit être utilisée que sur un
réseau privé maîtrisé.

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
     https://pasteberth.example.internal/api/zones/default/images
```

```json
{
  "id": "2026-08-25_01-22-31_a81c42.png",
  "filename": "2026-08-25_01-22-31_a81c42.png",
  "created_at": "2026-08-24T23:22:31.412000+00:00",
  "width": 1920, "height": 1080, "size": 9283, "format": "png",
  "preview_url": "/previews/default/2026-08-25_01-22-31_a81c42.png",
  "reference": "@/chemin/du/depot/storage/default/2026-08-25_01-22-31_a81c42.png"
}
```

Formats : PNG, JPEG, WebP — déterminés par le **contenu** (magic bytes +
structure), jamais par le MIME déclaré. Refus : vide, trop grand, format
inconnu, conteneur incomplet ou image corrompue. Les noms sont générés côté serveur
(`AAAA-MM-JJ_HH-MM-SS_<6 hex>.ext`, création `O_EXCL` : zéro écrasement).
Un espace libre sous le seuil renvoie `507 storage_low`. Une erreur de
rétention renvoie `503 retention_error` après la création de l'image ; le
client doit donc recharger l'historique avant de retenter aveuglément.

## Sécurité

- Mots de passe : scrypt salé (N=16384), comparaison temps constant,
  fichier `passwd` 0600 à côté de la configuration et ignoré par Git ; temporisation + verrouillage progressif par IP
  (honorant XFF seulement via proxy de confiance).
- Les requêtes de login sont limitées à 16 KiB, les vérifications scrypt sont
  globalement bornées et les uploads partagent un budget mémoire de 128 MiB ;
  les uploads restent limités à 20 MiB par défaut et 50 MiB au maximum.
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
- Répertoires privés recommandés (`0700`), images/sidecars privés (`0600`), liens
  symboliques refusés et réconciliation des temporaires après crash.
- Validation structurelle complète des PNG/JPEG/WebP, budget de dimensions et
  de pixels, et refus des conteneurs tronqués.
- Rétention sous verrou par zone : ordre déterministe, uploads concurrents
  sûrs (tests dédiés).

## Tests

```sh
npm ci
npm run test:all              # Python + navigateur en parallèle
# ciblé : python3 -m unittest discover -s tests -v
# ciblé : npm run test:e2e
```

La suite couvre : validation images (PNG/JPEG/WebP, corruption, spoofing),
configuration & politique de démarrage, stockage/rétention/ownership,
auth/sessions/anti-bruteforce, parser multipart, intégration HTTP complète
(auth, CSRF/Origin, proxys, en-têtes, fuite de secret), concurrence
(uploads parallèles même/multi zones, lecteurs pendant écritures),
CLI (passwd, refus de configuration dangereuse), contrats frontend, et quatre
scénarios navigateur Playwright sur un serveur Pasteberth réel : chargement et
sélection clavier, collage sans zone, upload/aperçu et glisser-déposer.
Les tests navigateur utilisent Chromium par défaut ; `E2E_BROWSER=firefox` est
disponible si le navigateur Playwright correspondant est installé.

## Limitations & V2

- Destination unique `local` (relative au serveur). L'abstraction
  `Destination` est prête pour une `SshDestination` (SFTP, credentials
  restant côté serveur).
- Pas d'extension navigateur : l'API est utilisable telle quelle, mais le
  CORS dédié sera ajouté explicitement le moment venu.
- Sessions en mémoire : un redémarrage déconnecte (volontaire, simple) ;
  suppression manuelle d'une image hors rétention à prévoir en UI.
- Mono-utilisateur par conception ; TLS délégué au reverse proxy.
