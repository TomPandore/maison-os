# Maison OS

La domotique de la maison, légère et pensée pour être pilotée par un agent (Claude, par MCP),
sans réécrire les protocoles : Maison OS se branche sur les briques officielles que Home
Assistant utilise lui-même.

> **Version 0.1 — socle + Matter.** Tuya, ESPHome, Google Cast, Riley et le tableau de bord
> arrivent ensuite.

## Comment c'est construit

```
 Claude ─► MCP /mcp          scripts ─► REST /api/*        navigateur ─► / (page d'état), SSE /api/events
                    └──────────────┬──────────────┘
                     garde Host · code de la maison
 ┌─────────────────────────────────▼─────────────────────────────────────┐
 │ Hub : état, bus d'événements, GARDE, demandes à valider, journal,      │
 │       étiquettes (nom, pièce), pilotes supervisés                     │
 └───────────────▲───────────────────────────────────────────────────────┘
                 │
   pilote matter ─► Matter Server (module officiel, matter.js) ─► les ampoules
```

- **Identité stable** : un appareil s'appelle `matter:<UniqueID>`, jamais d'après son nom.
  Le nom et la pièce sont des étiquettes (`data/labels.json`).
- **La garde** : une personne (avec le code), un automatisme validé et la maison sont
  toujours obéis. Un agent (Claude, Riley, un script) est **retenu** dans une pièce protégée,
  aux heures calmes, ou sur un appareil sans pièce : sa demande attend qu'une personne la
  valide avec le code.
- **Le journal** : chaque ordre, validation et étiquette (`data/journal.jsonl`).
- **Un pilote qui plante redémarre** ; ses appareils passent « injoignable » entre-temps.

## Matter sans réappairer

Le réseau Matter est tenu par le **Matter Server** (le module officiel déjà installé sur le
Pi). Home Assistant lui parle en WebSocket ; Maison OS fait pareil, en parallèle. Les
ampoules ne sont ni réappairées, ni dérangées. Lecture seule par défaut
(`matter_read_only: true`) jusqu'à ce qu'on décide de piloter.

## Installer sur le Pi (module Home Assistant)

1. Ajouter ce dépôt dans **Paramètres → Modules → Boutique → ⋮ → Dépôts**
   (ou copier le dossier `maison_os/` dans `/addons` du Pi).
2. Installer **Maison OS**, puis dans **Configuration** : choisir le `pin` (6 chiffres ou
   plus), la liste des `protected_rooms`, et éventuellement `quiet_start` / `quiet_end`.
3. Démarrer. Page d'état : `http://homeassistant.local:8790`.
4. Si le journal du module dit qu'il ne joint pas le Matter Server, essayer
   `matter_url: ws://172.30.32.1:5580/ws`.
5. **Reprendre les pièces et les noms de Home Assistant** : sur la page d'état, « Voir les
   propositions ». Maison lit les registres de HA (lecture seule), retrouve chaque appareil
   par son identifiant Matter ou son numéro de série, et propose nom et pièce. Cocher, taper
   le code, « Appliquer ».

## Brancher Claude

Le MCP est sur `http://homeassistant.local:8790/mcp`. Dans l'appli Claude sur le Mac
(Réglages → Développeur → configuration MCP) :

```json
{ "mcpServers": { "maison": { "command": "npx", "args": ["-y", "mcp-remote", "http://homeassistant.local:8790/mcp", "--allow-http"] } } }
```

Outils : `house_overview`, `get_device`, `set_device`, `pending_approvals`,
`recent_journal`, `set_label`.

## API

| Route | |
|---|---|
| `GET /api/health` | état des pilotes |
| `GET /api/devices`, `GET /api/devices/{id}` | appareils et points |
| `GET /api/rooms` | pièces → appareils |
| `POST /api/command` `{device_id, key, value}` | un ordre (200 fait, 202 retenu, 400 refusé) ; `x-maison-origin: ui` + `pin` = une personne |
| `GET /api/approvals`, `POST /api/approvals/{id}` `{approve, pin}` | demandes retenues |
| `PUT /api/labels/{id}` `{name?, room?, hidden?, pin}` | étiquettes |
| `GET /api/journal?limit=` | journal |
| `GET /api/import/homeassistant` | propositions de noms et pièces lues dans HA (rien n'est appliqué) |
| `POST /api/import/homeassistant` `{device_ids, pin}` | applique les propositions cochées |
| `GET /api/events` | flux en direct (SSE) |

## Développer

```sh
cd maison_os
pip install -e ".[dev]"
python -m pytest                          # 15 tests, dont un faux Matter Server chargé des vraies données
python -m tests.fake_matter_server 5580   # pour essayer sans le Pi
echo 123456 | python -m maison set-pin && python -m maison serve
```

Configuration hors module : `data/maison.yaml` (voir `maison/config.py`, `DEFAULTS`).
