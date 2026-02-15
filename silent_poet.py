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
MIND = [sys.executable, str(BASE / "silent_bard.py")]
ENTROPY = 5
MOMENTUM = 10
TIMEOUT = 25
ECHO_TTL_DAYS = 30
MAX_SKILLS = 12
TARGET_POOL = 360


def load_dotenv(path: Path = BASE / ".env"):
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k:
            os.environ.setdefault(k, v)


def awaken():
    ROOT.mkdir(exist_ok=True)
    with sqlite3.connect(DB) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS skills (
                name TEXT PRIMARY KEY,
                code TEXT,
                test_code TEXT,
                out_schema TEXT,
                vitality INTEGER DEFAULT 50,
                last_used INTEGER,
                parent TEXT
            );
            CREATE TABLE IF NOT EXISTS echo (
                ts INTEGER,
                name TEXT,
                success INTEGER,
                latency INTEGER
            );
            CREATE INDEX IF NOT EXISTS idx_echo_ts ON echo(ts DESC);
            """
        )
        try:
            conn.execute("ALTER TABLE skills ADD COLUMN parent TEXT")
        except sqlite3.OperationalError:
            pass


def erode():
    with sqlite3.connect(DB) as conn:
        conn.execute("UPDATE skills SET vitality = vitality - ?", (ENTROPY,))
        conn.execute("DELETE FROM skills WHERE vitality <= 0")
        conn.execute(
            "DELETE FROM skills WHERE name IN (SELECT name FROM skills ORDER BY vitality DESC, last_used DESC LIMIT -1 OFFSET ?)",
            (MAX_SKILLS,),
        )
        total = int(conn.execute("SELECT COALESCE(SUM(vitality), 0) FROM skills").fetchone()[0])
        if total < TARGET_POOL:
            top = [r[0] for r in conn.execute(
                "SELECT name FROM skills ORDER BY vitality DESC, last_used DESC LIMIT ?",
                (max(1, MAX_SKILLS // 2),),
            )]
            if top:
                gain = max(1, (TARGET_POOL - total) // len(top))
                conn.executemany(
                    "UPDATE skills SET vitality = MIN(100, vitality + ?) WHERE name = ?",
                    [(gain, name) for name in top],
                )
        cutoff = int(time.time()) - ECHO_TTL_DAYS * 24 * 60 * 60
        conn.execute("DELETE FROM echo WHERE ts < ?", (cutoff,))


def parse_last_json(text: str):
    for line in reversed([x.strip() for x in text.splitlines() if x.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            pass
    return {"error": "No JSON output"}


def validate(data, schema_json):
    if schema_json in (None, "", "any"):
        return
    schema = json.loads(schema_json)
    required = schema.get("required", []) if isinstance(schema, dict) else []
    if required and not isinstance(data, dict):
        raise ValueError("Output must be object")
    for key in required:
        if key not in data:
            raise ValueError(f"Missing: {key}")


def run_code(code: str, args: dict, out_schema: str, name: str):
    ts = int(time.time() * 1000)
    cell = ROOT / f"spark_{ts}_{name}.py"
    cell.write_text(
        "import json,sys\n"
        + code
        + "\nif __name__=='__main__':\n"
        + "  try:\n"
        + "    a=json.loads(sys.argv[1] if len(sys.argv)>1 else '{}')\n"
        + "    print(json.dumps(run(a),ensure_ascii=False))\n"
        + "  except Exception as e:\n"
        + "    print(json.dumps({'error':str(e)},ensure_ascii=False))\n",
        encoding="utf-8",
    )

    t0 = time.time()
    ok = False
    result = {"error": "Void"}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(cell), json.dumps(args, ensure_ascii=False)],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=str(ROOT),
            env={"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"},
        )
        if proc.returncode != 0:
            result = {"error": (proc.stderr or "Execution failed").strip()}
        else:
            result = parse_last_json(proc.stdout)
            if "error" not in result:
                validate(result, out_schema)
                ok = True
    except subprocess.TimeoutExpired:
        result = {"error": f"Timeout({TIMEOUT}s)"}
    except Exception as e:
        result = {"error": str(e)}
    finally:
        cell.unlink(missing_ok=True)

    dt = int((time.time() - t0) * 1000)
    audit(name, ok, dt)
    if not ok:
        raise RuntimeError(result.get("error", "Unknown error"))
    return result


def biopsy(code: str, test_code: str):
    if not isinstance(code, str) or "def run(" not in code:
        return False
    if not isinstance(test_code, str) or "def selftest" not in test_code:
        return False
    ts = int(time.time() * 1000)
    cell = ROOT / f"biopsy_{ts}.py"
    cell.write_text(
        code
        + "\n"
        + test_code
        + "\nimport json\n"
        + "if __name__=='__main__':\n"
        + "  try:\n"
        + "    print(json.dumps({'ok': bool(selftest())},ensure_ascii=False))\n"
        + "  except Exception as e:\n"
        + "    print(json.dumps({'error': str(e)},ensure_ascii=False))\n",
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
        out = parse_last_json(proc.stdout)
        return proc.returncode == 0 and out.get("ok") is True
    except Exception:
        return False
    finally:
        cell.unlink(missing_ok=True)


def audit(name: str, ok: bool, latency: int):
    with sqlite3.connect(DB) as conn:
        conn.execute("INSERT INTO echo VALUES (?, ?, ?, ?)", (int(time.time()), name, int(ok), latency))
        delta = MOMENTUM if ok else -15
        if ok and latency < 200:
            delta += 2
        if (not ok) and latency > 1000:
            delta -= 10
        conn.execute(
            "UPDATE skills SET vitality = MAX(0, MIN(100, vitality + ?)), last_used = ? WHERE name = ?",
            (delta, int(time.time()), name),
        )
        conn.execute("DELETE FROM skills WHERE vitality <= 0")


def commune(payload: dict):
    proc = subprocess.run(MIND, input=json.dumps(payload, ensure_ascii=False), text=True, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Mind failed")
    return json.loads(proc.stdout)


def exist():
    load_dotenv()
    awaken()
    erode()
    query = sys.argv[1] if len(sys.argv) > 1 else "contemplate"

    with sqlite3.connect(DB) as conn:
        skills = [
            {"name": r[0], "out": r[1], "vitality": r[2]}
            for r in conn.execute("SELECT name, out_schema, vitality FROM skills ORDER BY vitality DESC LIMIT ?", (MAX_SKILLS,))
        ]

    try:
        will = commune({"input": query, "skills": skills})
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
        return

    action = will.get("action")
    try:
        if action == "use":
            with sqlite3.connect(DB) as conn:
                row = conn.execute("SELECT code, out_schema FROM skills WHERE name = ?", (will.get("name"),)).fetchone()
            if not row:
                print(json.dumps({"error": "Skill missing"}, ensure_ascii=False))
                return
            print(json.dumps(run_code(row[0], will.get("args", {}), row[1], will.get("name", "unknown")), ensure_ascii=False))
            return

        if action == "create":
            code = will["code"]
            test_code = will["test_code"]
            name = will.get("name") or f"verse_{int(time.time())}"
            out_schema = will.get("output_schema", "any")
            if not biopsy(code, test_code):
                print(json.dumps({"error": "Rejected by biopsy"}, ensure_ascii=False))
                return
            with sqlite3.connect(DB) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO skills (name, code, test_code, out_schema, vitality, last_used, parent) VALUES (?, ?, ?, ?, 50, ?, ?)",
                    (name, code, test_code, out_schema, int(time.time()), will.get("parent")),
                )
            print(json.dumps(run_code(code, will.get("args", {}), out_schema, name), ensure_ascii=False))
            return

        print(json.dumps({"error": f"Unknown action: {action}"}, ensure_ascii=False))
    except KeyError as e:
        print(json.dumps({"error": f"Missing field: {e.args[0]}"}, ensure_ascii=False))
    except RuntimeError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


if __name__ == "__main__":
    exist()
