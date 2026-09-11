# Plan d'implementation : tokens d'acces

## Statut et reference

- Plan persistant pour l'implementation de la gestion des tokens Pasteberth.
- Depot : `/home/devint3/Depots/pasteberth`.
- Revision de depart : `3e4610d` (`main` synchronise avec `origin/main`).
- Le worktree etait propre avant l'ajout de ce plan.
- Le commit du plan doit preceder toute modification fonctionnelle.
- Aucun token ne doit etre stocke en clair dans le depot, la configuration ou le
  registre persistant.

## Decisions validees

### Administration

- Toute session authentifiee par le mot de passe global est administratrice.
- Les tokens ne peuvent pas creer, modifier, renouveler, suspendre ou revoquer
  d'autres tokens.
- Il n'existe pas d'identite individuelle des administrateurs ; l'audit ne peut
  donc pas attribuer une action a une personne precise.
- L'UI d'administration est reservee aux sessions admin.

### Transport et mode d'execution

- Le token est une credential d'API envoyee par
  `Authorization: Bearer <token>`.
- L'UI utilisateur par token est hors de la premiere version.
- Les tokens sont desactives lorsque `auth.enabled = false`.
- Les appels locaux filesystem et CLI qui n'utilisent pas le daemon restent
  hors du perimetre de cette autorisation Web.
- Un token ne doit jamais etre accepte dans une URL, un cookie navigateur ou
  un log.

### Permissions

- Les permissions elementaires sont `L`, `R`, `W`.
- `L` : lister les zones et les metadonnees des fichiers.
- `R` : lire les octets et produire une archive.
- `W` : uploader, remplacer, supprimer et modifier les commentaires.
- Les profils d'interface sont `RO = L+R` et `RW = L+R+W`.
- Les grants personnalises peuvent utiliser n'importe quelle combinaison, y
  compris `L=R=W=0`.
- Un grant sans aucun droit peut seulement verifier l'existence de sa cible.
- `R` sans `L` reste possible pour lire un nom de fichier connu.
- Une copie exige `R` sur la source et `W` sur la cible.
- Un deplacement exige `R+W` sur la source et `W` sur la cible.
- Les droits d'un token sont cumulatifs entre plusieurs grants.

### Scopes

- Un token peut avoir plusieurs grants dans un seul secret.
- Un grant peut cibler :
  - une zone par son identifiant stable `zone_id` ;
  - un groupe par son nom actuel ;
  - un chemin absolu normalise (`PATH`) ;
  - toutes les zones (`global`).
- Le libelle d'une zone n'est jamais une cle d'autorisation.
- Un token de zone dont l'ID n'est plus actif reste conserve et est signale
  comme inactif dans le panneau d'administration.
- Un token de groupe suit dynamiquement les membres du groupe.
- Un renommage de groupe detache les tokens qui utilisaient l'ancien nom.
- Un token global inclut les zones futures.
- Un PATH suit la zone actuellement configuree sur ce chemin, ce qui permet la
  reallocation d'un ID sur le disque.
- Un PATH non resolu ou ambigu est inactif et ne donne aucun acces.

### Suspension et cycle de vie

- Les tokens survivent aux redemarrages et a la rotation du mot de passe global.
- La revocation individuelle est obligatoire.
- L'administrateur peut suspendre globalement l'usage des tokens.
- Il peut suspendre une zone ; tous les tokens visant cette zone sont alors
  refuses.
- Il peut suspendre un groupe ; tous les tokens visant ses zones membres sont
  alors refuses, y compris les grants directs de zone.
- Les suspensions sont persistantes et ont priorite sur les grants.
- La prolongation conserve le secret et fixe l'expiration a maintenant plus la
  duree demandee.
- La rotation revoque atomiquement l'ancien secret, conserve le libelle et les
  grants, genere un nouveau secret et fixe sa nouvelle expiration a maintenant
  plus la duree demandee.
- Le secret n'est affiche qu'a la creation ou a la rotation ; il est ensuite
  impossible a recuperer.

### Upload et confidentialite pratique

- Le stockage conserve le comportement standard Pasteberth : deduplication par
  contenu et remplacement nomme explicite.
- La politique `allow_replace` est attachee au grant/token ; le client ne peut
  pas l'elever avec un champ d'upload.
- Pour un token sans `R`, la reponse d'upload ne doit pas signaler explicitement
  `duplicate`, `replaced`, `retention_deleted` ni exposer une reference de
  fichier.
- Il s'agit de confidentialite pratique, pas d'une garantie contre toutes les
  fuites temporelles ou les effets de quota.

## Modele de donnees propose

- Utiliser un registre persistant externe au bundle, recommande en SQLite via
  `sqlite3` de la bibliotheque standard.
- Configurer son chemin sous `[auth]`, avec une valeur par defaut externe au
  deploiement et compatible avec le service systemd.
- Table token : identifiant public, libelle, hash du secret, dates de creation,
  expiration, derniere rotation, revocation et etat.
- Table grants : token, type de scope, valeur de scope, bits `L/R/W`,
  `allow_replace`.
- Table suspensions : niveau global, zone, groupe, etat et dates de modification.
- Le secret utilise un selecteur public et une partie aleatoire de haute
  entropie ; seul le hash de la partie secrete est conserve.
- Les rotations, revocations et changements de suspension sont transactionnels.
- Le registre doit etre protege par permissions privees et verifie comme etat
  externe, sans suivre les sidecars des fichiers.

## Ordre d'implementation

1. Reconfirmer `git status`, la revision et le plan commite ; ne toucher a aucun
   fichier utilisateur sous `work/exchange2`.
2. Ajouter la configuration du registre et ses validations de chemin,
   permissions et mode `auth.enabled`.
3. Implementer le registre persistant : creation, hash, validation, expiration,
   revocation, prolongation, rotation et suspensions.
4. Introduire un principal HTTP distingue : session admin, token bearer ou
   requete anonyme refusee ; refuser les combinaisons ambiguës session/bearer.
5. Centraliser la resolution des scopes et la matrice L/R/W dans la couche Web.
   Reutiliser le registre actuel des zones et la resolution existante des
   groupes ; ne pas recopier ces regles dans les handlers.
6. Couvrir toutes les routes `/items` et `/images` : overview filtre, liste,
   contenu, archive, upload, commentaire, suppression et transfert.
7. Autoriser un transfert avec un seul token multi-grants en verifiant chaque
   zone independamment : source et cible ne doivent jamais etre assimilees.
8. Conserver `drop/resolve` et `regularize` hors des permissions implicites du
   bearer tant qu'une politique specifique n'est pas necessaire.
9. Ajouter une reponse d'upload redigee pour les tokens sans `R`, sans modifier
   la reponse des sessions admin.
10. Ajouter les endpoints admin de listing, creation, prolongation, rotation,
    suspension et revocation ; ne jamais retourner un secret existant.
11. Ajouter l'UI : bouton Access par zone, gestion groupe/global, etat absent ou
    suspendu, copie du secret a la creation, renouvellement, rotation et
    revocation avec focus clavier et comportement mobile.
12. Ajouter le support bearer au client HTTP et les options CLI/MCP necessaires,
    sans mettre les credentials dans les arguments par defaut ou les URLs.
13. Mettre a jour API, configuration, deploiement, operations, integration et
    security documentation.
14. Executer les tests cibles apres chaque lot, puis les suites Python,
    navigateur et site avant toute release.

## Matrice HTTP initiale

- `GET /api/health` reste public et ne prouve pas l'autorisation d'une zone.
- `GET /api/zones` et `GET /api/groups` exigent `L` sur les zones retournees et
  ne doivent jamais exposer les zones hors scope.
- `GET /api/zones/{id}/items` exige `L`.
- `GET/HEAD .../content` et `POST .../archive` exigent `R`.
- `POST .../items` exige `W` ; `allow_replace` vient du grant.
- `PATCH .../comment`, `DELETE`, `batch-delete` exigent `W`.
- `POST /api/transfers` exige les droits necessaires sur source et cible.
- Les routes d'administration des tokens exigent une session admin, jamais un
  bearer token.
- Un endpoint d'existence dedie devra permettre le cas `L=R=W=0` sans retourner
  une vue globale du registre.

## Tests a ajouter

- Hash, entropie, parsing strict et absence de secret en clair.
- Persistance apres recreation du service et rotation du mot de passe.
- Expiration, token permanent, prolongation depuis maintenant et rotation
  atomique.
- Revocation individuelle, suspension globale, de zone et de groupe.
- Scopes par ID, groupe, PATH, zones absentes, groupe renomme et PATH ambigu.
- Union de grants et refus prioritaire des suspensions.
- Matrice L/R/W sur chaque route legacy et generique.
- Copie avec `R` source + `W` cible et deplacement avec `RW` source + `W` cible.
- Refus de privilege escalation via `replace`, query string, cookie ou header
  ambigu.
- Reponses write-only sans `duplicate`, `replaced`, reference ou retention.
- Concurrence de creation, rotation, revocation et suspension.
- Permissions du registre, fichiers SQLite auxiliaires et reprise apres erreur.
- UI Access, secret affiche une seule fois, tokens absents, suspensions,
  rotation, prolongation, revocation, responsive et accessibilite.
- Client HTTP, CLI, MCP, URL prefix, reverse proxy et documentation executable.

## Risques et limites

- Un groupe est actuellement identifie par son `name`, pas par un ID immuable.
- La resolution PATH doit rester coherente entre POSIX, Windows, symlinks et
  chemins realloues.
- Les droits de lecture de metadonnees peuvent encore reveler des informations
  meme sans lecture des octets ; ce comportement doit rester documente.
- Les tokens permanents augmentent l'importance de la revocation et des
  sauvegardes du registre.
- Le mot de passe partage ne permet aucun audit individuel fiable.
- La premiere version ne fournit pas de login UI restreint par token.

## Estimation

- Registre et cycle de vie : 7 a 10 jours-homme.
- Scopes groupe/global/PATH, grants multiples, suspensions et transferts : 5 a
  8 jours-homme.
- UI, client, documentation et tests : 6 a 9 jours-homme.
- Total attendu : 18 a 27 jours-homme pour une version complete.

## Verification et checkpoints

- Avant chaque lot : `git status --short`, `git diff --check`,
  `git rev-parse HEAD`.
- Utiliser uniquement `work/tmp/` pour les temporaires de test.
- Ne jamais annuler des changements faits par l'utilisateur ou un autre agent.
- Ne pas mettre a jour la version ni publier une release avant la suite complete
  de verification.
- Le commit initial du plan est le point de restauration de l'etat fonctionnel
  avant implementation.
