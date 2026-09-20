import argparse
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

from .config import ROOT, Settings


def build(settings):
    from .engine import Engine
    from .memory import LocalEmbedder, Memory
    from .model import ClaudeModel
    from .store import Store
    from .transport import MockTransport, WeixinTransport

    db = Store(settings.data_dir / "cc-chat.sqlite3")
    return Engine(
        db,
        settings,
        ClaudeModel(settings),
        Memory(db, settings, LocalEmbedder(settings)),
        MockTransport() if settings.transport == "mock" else WeixinTransport(settings),
    )


def serve(settings):
    import uvicorn

    from .web import make_app

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    handle = (settings.data_dir / "runtime.lock").open("a+b")
    handle.seek(0)
    handle.write(b"0")
    handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit("cc-chat 已有运行中的后台实例")
    (settings.data_dir / "service.pid").write_text(str(os.getpid()), "utf-8")
    try:
        uvicorn.run(make_app(build(settings)), host="127.0.0.1", port=settings.port, access_log=False)
    finally:
        handle.close()


def main():
    parser = argparse.ArgumentParser(prog="cc-chat")
    parser.add_argument("--config", type=Path)
    subs = parser.add_subparsers(dest="command", required=True)
    for name in ["serve", "start", "status", "prepare-model", "install-startup", "stop"]:
        subs.add_parser(name)
    switch = subs.add_parser("migrate")
    switch.add_argument("--session", required=True)
    switch.add_argument("--cc-config", type=Path)
    rollback = subs.add_parser("restore")
    rollback.add_argument("backup", type=Path)
    rollback.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.config:
        os.environ["CC_CHAT_CONFIG"] = str(args.config.resolve())
    s = Settings.load(args.config)
    if args.command == "serve":
        serve(s)
    elif args.command == "start":
        import urllib.request

        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            if json.load(opener.open(f"http://127.0.0.1:{s.port}/health", timeout=2))["service"] == "cc-chat":
                print("cc-chat 已经运行")
                return
        except OSError:
            pass
        s.data_dir.mkdir(parents=True, exist_ok=True)
        log = (s.data_dir / "service.log").open("ab")
        child_args = [sys.executable, "-m", "cc_chat.cli"]
        if args.config:
            child_args += ["--config", str(args.config.resolve())]
        p = subprocess.Popen(
            child_args + ["serve"],
            cwd=ROOT,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
        print(
            json.dumps(
                {"pid": p.pid, "url": f"http://127.0.0.1:{s.port}", "log": str(s.data_dir / "service.log")}
            )
        )
    elif args.command == "status":
        from .store import Store

        db = Store(s.data_dir / "cc-chat.sqlite3")
        print(
            json.dumps(
                {
                    "transport": s.transport,
                    "persona": (db.persona() or {}).get("name"),
                    "error": db.get("last_error"),
                    "paused": db.get("paused"),
                    "url": f"http://127.0.0.1:{s.port}",
                },
                ensure_ascii=False,
            )
        )
    elif args.command == "prepare-model":
        from .memory import LocalEmbedder

        s.embedding_local_only = False
        vectors = asyncio.run(
            LocalEmbedder(s).encode(["今天的课程很有意思。", "这堂课挺有趣的。", "咖啡不要加糖。"])
        )
        print(
            json.dumps(
                {
                    "model": s.embedding_model,
                    "dimensions": len(vectors[0]),
                    "synonym_cosine": sum(a * b for a, b in zip(vectors[0], vectors[1])),
                    "unrelated_cosine": sum(a * b for a, b in zip(vectors[0], vectors[2])),
                }
            )
        )
    elif args.command == "migrate":
        from .integration import migrate

        print(json.dumps(migrate(s, args.session, args.cc_config), ensure_ascii=False))
    elif args.command == "restore":
        from .integration import restore

        print(json.dumps(restore(args.backup, args.force), ensure_ascii=False))
    elif args.command == "install-startup":
        from .integration import install_startup

        print(json.dumps(install_startup()))
    elif args.command == "stop":
        # Authenticated shutdown endpoint isn't exposed to the browser; verify process identity first.
        pidfile = s.data_dir / "service.pid"
        if not pidfile.exists():
            return
        pid = int(pidfile.read_text())
        if os.name == "nt":
            script = f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; if($p.CommandLine -match 'cc_chat.cli.*serve'){{Stop-Process -Id {pid}}}"
            subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
        else:
            import signal

            os.kill(pid, signal.SIGTERM)


if __name__ == "__main__":
    main()
