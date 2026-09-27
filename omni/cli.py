"""`omni`: talk to omnid from a terminal or a hotkey.

  omni ask "open files on the left"      stream a reply (add --speak to hear it)
  omni listen                            push-to-talk: start listening now (hotkey)
  omni stop                              stop speaking and interrupt the current turn
  omni status | doctor | new             state, setup checks, fresh conversation
  omni voice continuous|wake [on|off]    toggle hands-free listening or the wake word
  omni memory [search QUERY]             show notes or search memory and past turns
  omni key elevenlabs                    store the ElevenLabs API key in the keyring
  omni app | popover                     open the full window or the quick-ask popover
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
import time

from .client import Client, DaemonUnavailable, call


def ask(text: str, speak: bool, show_tools: bool) -> int:
    started = time.monotonic()
    code = 0
    with Client() as client:
        for message in client.stream("ask", text=text, speak=speak, source="cli"):
            event = message.get("event")
            if event == "delta":
                print(message["text"], end="", flush=True)
            elif event == "tool" and show_tools and message.get("status") == "running":
                print(f"\x1b[2m[{message['type']}] {message['label']}\x1b[0m", file=sys.stderr)
            elif event == "prompt":
                answer_prompt(client, message)
            elif "ok" in message:
                result = message.get("result") or {}
                if not message["ok"]:
                    print(f"\nomni: {message.get('error')}", file=sys.stderr)
                    return 1
                if result.get("status") != "completed":
                    print(f"\n[{result.get('status')}] {result.get('error', '')}", file=sys.stderr)
                    code = 1
                timings = result.get("timings", {})
                print(f"\n\x1b[2m{time.monotonic() - started:.1f}s · first token {timings.get('first_token', '?')} ms · "
                      f"tokens in {result.get('tokens', {}).get('input', 0)} (cached {result.get('tokens', {}).get('cached', 0)})\x1b[0m",
                      file=sys.stderr)
    return code


def answer_prompt(client: Client, prompt: dict) -> None:
    if not sys.stdin.isatty():
        return
    if prompt["kind"] == "approval":
        reply = input(f"\nOmni wants to {prompt['text']}. Allow? [y/N] ").strip().lower()
        client.send("answer", prompt=prompt["id"], value="accept" if reply in ("y", "yes") else "decline")
    else:
        client.send("answer", prompt=prompt["id"], value=input(f"\n{prompt['text']} "))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omni", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("ask")
    p.add_argument("text", nargs="+")
    p.add_argument("--speak", action="store_true")
    p.add_argument("--quiet-tools", action="store_true")
    for name in ("listen", "stop", "status", "doctor", "new", "app", "app-background", "popover", "daemon"):
        sub.add_parser(name)
    p = sub.add_parser("voice")
    p.add_argument("setting", choices=["continuous", "wake"])
    p.add_argument("value", nargs="?", choices=["on", "off"])
    p = sub.add_parser("memory")
    p.add_argument("action", nargs="?", choices=["search"])
    p.add_argument("query", nargs="*")
    p = sub.add_parser("key")
    p.add_argument("service", choices=["elevenlabs"])
    args = parser.parse_args(argv)

    if args.command == "daemon":
        from .daemon import main as daemon_main
        daemon_main()
        return 0
    if args.command in ("app", "app-background", "popover"):
        from .app import main as app_main
        return app_main({"app": [], "app-background": ["--background"], "popover": ["--popover"]}[args.command])
    if args.command == "key":
        from .voice.speak import store_elevenlabs_key
        store_elevenlabs_key(getpass.getpass("ElevenLabs API key: "))
        print("Saved to the keyring.")
        return 0
    try:
        if args.command == "ask":
            return ask(" ".join(args.text), args.speak, not args.quiet_tools)
        if args.command == "listen":
            call("listen")
        elif args.command == "stop":
            call("interrupt")
        elif args.command == "new":
            call("new_thread")
            print("Started a fresh conversation.")
        elif args.command == "voice":
            value = None if args.value is None else args.value == "on"
            print(json.dumps(call("voice", setting=args.setting, value=value), indent=2))
        elif args.command == "memory":
            if args.action == "search":
                print(json.dumps(call("memory.search", query=" ".join(args.query)), indent=2, default=str))
            else:
                for store, entries in call("memory.list").items():
                    print(f"# {store}")
                    for entry in entries:
                        print(f"- [{entry['id']}] {entry['text']}")
        elif args.command == "doctor":
            for name, status, hint in call("doctor"):
                print(f"{'✓' if status == 'ok' else '!' if status == 'warn' else '✗'} {name}: {hint}")
        else:
            print(json.dumps(call("status"), indent=2, default=str))
    except DaemonUnavailable as exc:
        print(f"omni: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"omni: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
