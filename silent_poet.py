import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE / "cortex"
DB = BASE / "memory.db"
BARD = [sys.executable, str(BASE / "silent_bard.py")]
QUESTION = "What should remain after repeated forgetting?"
DECAY = 1
REWARD = 2
PENALTY = 2
LIMIT = 9
TIMEOUT = 15


def load_env():
    path = BASE / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def awaken():
    ROOT.mkdir(exist_ok=True)
    with sqlite3.connect(DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skills(
                name TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                test_code TEXT NOT NULL,
                vitality INTEGER DEFAULT 3,
                last_used INTEGER
            )
            """
        )
        conn.execute("UPDATE skills SET vitality = vitality - ?", (DECAY,))
        conn.execute("DELETE FROM skills WHERE vitality <= 0")
        conn.execute(
            "DELETE FROM skills WHERE name IN (SELECT name FROM skills ORDER BY vitality DESC, last_used DESC LIMIT -1 OFFSET ?)",
            (LIMIT,),
        )


def parse_json(text):
    for line in reversed([x.strip() for x in text.splitlines() if x.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            pass
    return {"error": "No JSON output"}


def spark(code, args, name):
    cell = ROOT / f"{name}_{int(time.time() * 1000)}.py"
    cell.write_text(
        "import json,sys\n"
        + code
        + "\nif __name__ == '__main__':\n"
        + "  try:\n"
        + "    args=json.loads(sys.argv[1] if len(sys.argv)>1 else '{}')\n"
        + "    print(json.dumps(run(args), ensure_ascii=False))\n"
        + "  except Exception as e:\n"
        + "    print(json.dumps({'error': str(e)}, ensure_ascii=False))\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(cell), json.dumps(args, ensure_ascii=False)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=str(ROOT),
            env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
        )
        out = parse_json(proc.stdout if proc.returncode == 0 else proc.stderr)
        if proc.returncode != 0 or "error" in out:
            raise RuntimeError(out.get("error", "Execution failed"))
        return out
    finally:
        cell.unlink(missing_ok=True)


def biopsy(code, test):
    if "def run(" not in code or "def selftest" not in test:
        return False
    cell = ROOT / f"biopsy_{int(time.time() * 1000)}.py"
    cell.write_text(
        code
        + "\n"
        + test
        + "\nimport json\n"
        + "if __name__ == '__main__':\n"
        + "  try:\n"
        + "    print(json.dumps({'ok': bool(selftest())}))\n"
        + "  except Exception as e:\n"
        + "    print(json.dumps({'error': str(e)}))\n",
        encoding="utf-8",
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(cell)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=str(ROOT),
            env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
        )
        return proc.returncode == 0 and parse_json(proc.stdout).get("ok") is True
    except Exception:
        return False
    finally:
        cell.unlink(missing_ok=True)


def touch(name, delta):
    with sqlite3.connect(DB) as conn:
        conn.execute(
            "UPDATE skills SET vitality = MAX(0, MIN(9, vitality + ?)), last_used = ? WHERE name = ?",
            (delta, int(time.time()), name),
        )
        conn.execute("DELETE FROM skills WHERE vitality <= 0")


def remember(name, code, test):
    with sqlite3.connect(DB) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO skills(name, code, test_code, vitality, last_used) VALUES(?, ?, ?, 3, ?)",
            (name, code, test, int(time.time())),
        )


def commune(query):
    with sqlite3.connect(DB) as conn:
        skills = [
            {"name": name, "vitality": vitality}
            for name, vitality in conn.execute(
                "SELECT name, vitality FROM skills ORDER BY vitality DESC, last_used DESC LIMIT ?",
                (LIMIT,),
            )
        ]
    proc = subprocess.run(
        BARD,
        input=json.dumps({"question": QUESTION, "input": query, "skills": skills}, ensure_ascii=False),
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Bard failed")
    return json.loads(proc.stdout)


def main():
    load_env()
    awaken()
    query = sys.argv[1] if len(sys.argv) > 1 else "contemplate"
    try:
        plan = commune(query)
        if plan.get("action") == "use":
            with sqlite3.connect(DB) as conn:
                row = conn.execute("SELECT code FROM skills WHERE name = ?", (plan.get("name"),)).fetchone()
            if not row:
                raise RuntimeError("Skill missing")
            out = spark(row[0], plan.get("args", {}), plan["name"])
            touch(plan["name"], REWARD)
        elif plan.get("action") == "create":
            if not biopsy(plan["code"], plan["test_code"]):
                raise RuntimeError("Rejected by biopsy")
            name = plan.get("name") or f"verse_{int(time.time())}"
            remember(name, plan["code"], plan["test_code"])
            out = spark(plan["code"], plan.get("args", {}), name)
            touch(name, REWARD)
        else:
            raise RuntimeError("Unknown action")
        print(json.dumps(out, ensure_ascii=False))
    except Exception as e:
        if isinstance(locals().get("plan"), dict) and plan.get("name"):
            touch(plan["name"], -PENALTY)
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
