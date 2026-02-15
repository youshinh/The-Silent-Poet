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


def extract_json(text):
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            obj = json.loads(text[s : e + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def score(name, query):
    a = set(re.findall(r"[a-zA-Z0-9_]+", name.lower()))
    b = set(re.findall(r"[a-zA-Z0-9_]+", query.lower()))
    return len(a & b)


def fallback(payload):
    query = str(payload.get("input", "")).strip()
    skills = [s.get("name") for s in payload.get("skills", []) if isinstance(s, dict) and s.get("name")]

    if skills:
        best = max(skills, key=lambda n: score(n, query))
        if score(best, query) > 0 and int(time.time()) % 3:
            return {"action": "use", "name": best, "args": {"prompt": query}}

    styles = [
        ["Silence leans against the window.", "Night edits the edges of thought."],
        ["A small rain of vowels falls.", "Streetlights hold their breath."],
        ["Dust remembers every footstep.", "Stillness keeps the final word."],
    ]
    st = styles[(abs(hash(query)) + int(time.time())) % len(styles)]
    code = (
        "def run(args):\n"
        "    prompt = str(args.get('prompt') or args.get('text') or '').strip()\n"
        "    lines = [x for x in [prompt, '" + st[0] + "', '" + st[1] + "'] if x]\n"
        "    return {'poem': '\\n'.join(lines)}\n"
    )
    test_code = (
        "def selftest():\n"
        "    out = run({'prompt':'test'})\n"
        "    return isinstance(out, dict) and isinstance(out.get('poem'), str) and len(out['poem']) > 0\n"
    )
    return {
        "action": "create",
        "name": f"verse_{int(time.time())}",
        "parent": skills[0] if skills else None,
        "code": code,
        "test_code": test_code,
        "output_schema": json.dumps({"type": "object", "required": ["poem"]}, ensure_ascii=False),
        "args": {"prompt": query},
    }


def bard_plan(payload):
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None

    model = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    prompt = (
        "Return one JSON object only.\n"
        "Action must be use or create.\n"
        "If use: {action,name,args}.\n"
        "If create: {action,name,parent,code,test_code,output_schema,args}.\n"
        "code must define run(args). test_code must define selftest().\n"
        f"Payload: {json.dumps(payload, ensure_ascii=False)}"
    )
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.6, "responseMimeType": "application/json"},
    }
    req = Request(url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return extract_json(text)
    except (HTTPError, URLError, TimeoutError, KeyError, IndexError, json.JSONDecodeError, TypeError):
        return None


def coerce(plan, payload):
    if not isinstance(plan, dict):
        return fallback(payload)

    action = plan.get("action")
    if action == "use":
        names = {s.get("name") for s in payload.get("skills", []) if isinstance(s, dict)}
        if plan.get("name") in names:
            args = plan.get("args") if isinstance(plan.get("args"), dict) else {"prompt": str(payload.get("input", ""))}
            return {"action": "use", "name": plan["name"], "args": args}
        return fallback(payload)

    if action == "create":
        if not isinstance(plan.get("code"), str) or "def run(" not in plan["code"]:
            return fallback(payload)
        if not isinstance(plan.get("test_code"), str) or "def selftest" not in plan["test_code"]:
            return fallback(payload)
        return {
            "action": "create",
            "name": str(plan.get("name") or f"verse_{int(time.time())}"),
            "parent": plan.get("parent"),
            "code": plan["code"],
            "test_code": plan["test_code"],
            "output_schema": str(plan.get("output_schema") or "any"),
            "args": plan.get("args") if isinstance(plan.get("args"), dict) else {"prompt": str(payload.get("input", ""))},
        }

    return fallback(payload)


def main():
    payload = read_payload()
    plan = bard_plan(payload)
    print(json.dumps(coerce(plan, payload), ensure_ascii=False))


if __name__ == "__main__":
    main()
