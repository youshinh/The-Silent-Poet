#!/usr/bin/env python3
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def read_payload():
    try:
        data = json.loads(input() or "{}")
    except Exception:
        data = {}
    return data if isinstance(data, dict) else {"input": str(data)}


def pick_skill(skills, query):
    words = set(re.findall(r"[a-zA-Z0-9_]+", query.lower()))
    scored = []
    for skill in skills:
        name = skill.get("name", "")
        tokens = set(re.findall(r"[a-zA-Z0-9_]+", name.lower()))
        scored.append((len(words & tokens), skill.get("vitality", 0), name))
    best = max(scored, default=(0, 0, ""))
    return best[2] if best[0] > 0 else ""


def fallback(payload):
    query = str(payload.get("input", "")).strip()
    question = str(payload.get("question", "")).strip()
    skills = payload.get("skills", [])
    name = pick_skill(skills, query)
    if name and int(time.time()) % 2:
        return {"action": "use", "name": name, "args": {"prompt": query}}

    lines = [
        "What survives after repeated forgetting?",
        "What deserves memory when its maker is gone?",
        "What should outlive the tool that asked it?",
    ]
    line = lines[(abs(hash(query or question)) + int(time.time())) % len(lines)]
    code = (
        "def run(args):\n"
        "    prompt = str(args.get('prompt') or '').strip()\n"
        f"    question = {question!r}\n"
        f"    line = {line!r}\n"
        "    parts = [x for x in [prompt, line, question] if x]\n"
        "    return {'poem': '\\n'.join(parts)}\n"
    )
    test = (
        "def selftest():\n"
        "    out = run({'prompt': 'test'})\n"
        "    return isinstance(out, dict) and isinstance(out.get('poem'), str) and bool(out['poem'])\n"
    )
    return {
        "action": "create",
        "name": f"verse_{int(time.time())}",
        "code": code,
        "test_code": test,
        "args": {"prompt": query},
    }


def ask_model(payload):
    key = os.getenv("SILENT_BARD_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    model = os.getenv("SILENT_BARD_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-2.0-flash"
    req = Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
        data=json.dumps(
            {
                "contents": [{"role": "user", "parts": [{"text": (
                    "Return one JSON object.\n"
                    "Action: use or create.\n"
                    "If create, code must define run(args), test_code must define selftest().\n"
                    "Keep it small. Carry the question forward.\n"
                    f"Payload: {json.dumps(payload, ensure_ascii=False)}"
                )}]}],
                "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"},
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=20) as res:
            data = json.loads(res.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text[text.find("{"): text.rfind("}") + 1])
    except (HTTPError, URLError, TimeoutError, KeyError, IndexError, TypeError, json.JSONDecodeError):
        return None


def coerce(plan, payload):
    names = {x.get("name") for x in payload.get("skills", []) if isinstance(x, dict)}
    if isinstance(plan, dict) and plan.get("action") == "use" and plan.get("name") in names:
        return {"action": "use", "name": plan["name"], "args": plan.get("args") or {"prompt": payload.get("input", "")}}
    return fallback(payload)


def main():
    payload = read_payload()
    print(json.dumps(coerce(ask_model(payload), payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
