"""CLI демона: bgd apps / search / plan / status / serve."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bgd.api import Context, Service
from bgd.errors import BgdError
from bgd.model import AppKind


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bgd", description="Единая модель приложений ByteguruOS")
    parser.add_argument("--wcv-dir", default=Context.wcv_dir, help="каталог манифестов WCV")
    parser.add_argument("--registry", default=Context.registry_path, help="файл реестра совместимости")
    parser.add_argument("--flatpak-json", help="файл с выводом flatpak list --json (для повторов)")
    parser.add_argument("--desktop-dir", action="append", default=[], help="каталог .desktop-файлов")
    sub = parser.add_subparsers(dest="command")

    apps = sub.add_parser("apps", help="список всех приложений")
    apps.add_argument("--json", action="store_true")
    apps.set_defaults(func="apps")

    find = sub.add_parser("search", help="поиск по названию и тегам")
    find.add_argument("query")
    find.add_argument("--json", action="store_true")
    find.set_defaults(func="search")

    plan = sub.add_parser("plan", help="что будет выполнено при запуске")
    plan.add_argument("app_id")
    plan.add_argument("--json", action="store_true")
    plan.set_defaults(func="plan")

    status = sub.add_parser("status", help="состояние реестра и окружения")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func="status")

    serve = sub.add_parser("serve", help="запустить демон на unix-сокете")
    serve.add_argument("--socket", help="путь сокета (по умолчанию $XDG_RUNTIME_DIR/byteguru/apps.sock)")
    serve.set_defaults(func="serve")

    doctor = sub.add_parser("doctor", help="проверить читаемость манифестов")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func="doctor")
    return parser


def _context(args: argparse.Namespace) -> Context:
    flatpak_text = ""
    if args.flatpak_json:
        flatpak_text = Path(args.flatpak_json).read_text(encoding="utf-8")
    return Context(
        wcv_dir=args.wcv_dir,
        registry_path=args.registry,
        desktop_dirs=tuple(args.desktop_dir) or ("/usr/share/applications",),
        flatpak_list_json=flatpak_text,
    )


def _print(payload: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    if isinstance(payload, dict) and "error" in payload:
        print(f"ошибка: {payload['error']}", file=sys.stderr)
        return
    if isinstance(payload, list):
        for item in payload:
            print(f"{item['kind']:<10} {item['id']:<28} {item['name']}  [{item['coverage'] or '—'}]")
        return
    print(payload)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    context = _context(args)
    service = Service(context)
    try:
        if args.func == "apps":
            from bgd.api import _app_to_dict

            _print([_app_to_dict(app) for app in service.apps()], args.json)
        elif args.func == "search":
            from bgd.api import _app_to_dict

            _print([_app_to_dict(app) for app in service.find(args.query)], args.json)
        elif args.func == "plan":
            payload = service.launch_plan(args.app_id)
            _print(payload, args.json)
            return 0 if payload.get("ok") else 1
        elif args.func == "status":
            _print(service.status(), args.json)
        elif args.func == "doctor":
            problems = []
            for path in sorted(Path(args.wcv_dir).rglob("*.wcv")) if Path(args.wcv_dir).is_dir() else []:
                from wcmgr.manifest import validate_manifest_text

                issues = validate_manifest_text(path.read_text(encoding="utf-8", errors="replace"))
                if issues:
                    problems.append({"file": str(path), "problems": issues})
            _print({"manifests_checked": True, "broken": problems} if args.json else problems or ["все манифесты читаются"], args.json)
        elif args.func == "serve":
            from bgd.daemon import run

            return run(args.socket, context)
    except BgdError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ошибка чтения: {exc}", file=sys.stderr)
        return 2
    finally:
        service.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
