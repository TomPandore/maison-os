# Maison OS

Lire **`prive/BRIEF.md`** en entier avant toute chose (fichier privé, ignoré par git : il contient des données de la maison) : contexte, inventaire de la maison,
assistant Riley à reprendre, décisions prises, règles, feuille de route. Une v0.1 testée
existe déjà dans `maison_os/` : on la fait évoluer, on ne recommence pas.

- Tests : `cd maison_os && pip install -e ".[dev]" && python -m pytest` (tous verts avant
  chaque commit).
- Réponses en français, simples : Tomji n'est pas développeur Python.
- Jamais de secret ni de donnée personnelle dans le dépôt (il est public).
- Jamais de test sur un appareil de la chambre de l'enfant ; tester sur la lampe de bureau
  et remettre l'état d'avant.
- Toute action irréversible sur la maison ou sur Home Assistant : demander d'abord.
