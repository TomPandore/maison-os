"""`python -m maison [serve|set-pin|check|telephones|retirer ID] [--data DIR]`"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from .app import build_app, build_hub, default_data_dir
from .config import Config, PinGuard


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="maison")
    ap.add_argument("command", nargs="?", default="serve", choices=["serve", "set-pin", "check", "telephones", "retirer"])
    ap.add_argument("ident", nargs="?", help="pour « retirer » : l'identifiant du téléphone")
    ap.add_argument("--data", type=Path, default=None, help="dossier des données (défaut : ./data, ou /data en module HA)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
    cfg = Config.load(args.data or default_data_dir())

    if args.command == "set-pin":
        pin = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Code de la maison (6 chiffres ou plus) : ")
        try:
            PinGuard(cfg.data_dir).set(pin)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        print("Code enregistré (haché).")
        return 0

    if args.command in ("telephones", "retirer"):
        from .api.people import People
        people = People(cfg.data_dir, cfg.raw.get("people"))
        if args.command == "retirer":
            if not args.ident or not people.revoke(args.ident):
                print("téléphone introuvable", file=sys.stderr)
                return 1
            print(f"Téléphone {args.ident} retiré : sa clé ne marche plus.")
            return 0
        for ph in people.phones():
            print(f"{ph['id']}  {ph['person']:<12} {ph['label']}")
        return 0

    if args.command == "check":
        hub = build_hub(cfg)
        print(f"Config OK — données : {cfg.data_dir}, écoute : {cfg.raw['listen']}")
        print(f"Pilotes : {', '.join(hub.drivers) or 'aucun'} ; pièces protégées : {', '.join(sorted(hub.guard.protected_rooms)) or 'aucune'}")
        return 0

    import uvicorn

    host, port = cfg.listen
    uvicorn.run(build_app(cfg), host=host, port=port, log_level="info", proxy_headers=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
