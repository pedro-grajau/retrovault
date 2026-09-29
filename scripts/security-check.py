from pathlib import Path


ROOT = Path(__file__).parents[1]
SOURCE_ROOTS = (ROOT / "backend", ROOT / "frontend", ROOT / "scripts")
FORBIDDEN_REMOTE_APIS = (
    "api.openai.com",
    "graph.facebook.com",
    "api.stripe.com",
)
TEXT_SUFFIXES = {".html", ".j2", ".js", ".json", ".md", ".py", ".sh", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml"}

violations: list[str] = []
for source_root in SOURCE_ROOTS:
    for path in source_root.rglob("*"):
        if not path.is_file() or (path.suffix not in TEXT_SUFFIXES and not path.name.startswith(".env")):
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        if "node_modules" in path.parts or "backend/app/frontend" in path.as_posix():
            continue
        contents = path.read_text(errors="ignore")
        for host in FORBIDDEN_REMOTE_APIS:
            if host in contents:
                violations.append(f"{path.relative_to(ROOT)} references {host}")

if violations:
    raise SystemExit(
        "Paid external API references are forbidden in CI:\n" + "\n".join(violations)
    )

print("Security guard passed: no paid OpenAI, Meta or Stripe API endpoint is referenced")
