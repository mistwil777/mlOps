"""Launch a sovereign Docker stack for a project.

Reads config/projects/<project>.yaml, generates a .env file from the template,
then runs docker compose up -d in an isolated directory under infra/instances/<project>/.

isolation_level:
  standard  — bridge network with external access (LAN reachable)
  sovereign — internal bridge, no external network, all traffic stays on host
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

_TEMPLATE_DIR = Path("infra/docker/project-template")
_INSTANCES_DIR = Path("infra/instances")

# Port allocation — projects get sequential blocks of 10 ports starting here
_PORT_BASE = 9000
_BLOCK_SIZE = 10  # mlx=+0, qdrant=+1, mlflow=+2


def _next_port_block() -> int:
    """Find the first unused port block by scanning existing instances."""
    used = set()
    for env_file in _INSTANCES_DIR.glob("*/.env"):
        for line in env_file.read_text().splitlines():
            if line.startswith("MLX_PORT="):
                try:
                    base = int(line.split("=")[1]) - 0
                    used.add((base - _PORT_BASE) // _BLOCK_SIZE)
                except ValueError:
                    pass
    block = 0
    while block in used:
        block += 1
    return _PORT_BASE + block * _BLOCK_SIZE


def launch(project: str, force: bool = False) -> Path:
    cfg_path = Path("config/projects") / f"{project}.yaml"
    if not cfg_path.exists():
        raise FileNotFoundError(f"Project config not found: {cfg_path}")

    cfg = yaml.safe_load(cfg_path.read_text())
    isolation = cfg.get("isolation_level", "standard")
    classification = cfg.get("classification", "interne")

    # Enforce sovereign for high-classification projects
    if classification in ("confidentiel", "secret") and isolation != "sovereign":
        print(f"WARNING: classification={classification} — overriding isolation_level to sovereign")
        isolation = "sovereign"

    instance_dir = _INSTANCES_DIR / project
    if instance_dir.exists():
        if not force:
            raise FileExistsError(
                f"Instance already exists: {instance_dir}\n"
                "Use --force to redeploy (will stop + replace existing stack)."
            )
        print(f"  Stopping existing stack for {project}…")
        subprocess.run(["docker", "compose", "down"], cwd=instance_dir, check=False)
        shutil.rmtree(instance_dir)

    instance_dir.mkdir(parents=True)
    shutil.copy(_TEMPLATE_DIR / "docker-compose.yml", instance_dir / "docker-compose.yml")

    port_base = _next_port_block()
    repo_root = Path(".").resolve()

    env_vars = {
        "PROJECT_NAME": project,
        "MLX_PORT":     str(port_base),
        "QDRANT_PORT":  str(port_base + 1),
        "MLFLOW_PORT":  str(port_base + 2),
        "MODELS_PATH":  str(repo_root / "models"),
        "ADAPTERS_PATH": str(repo_root / "adapters"),
        "ARTIFACTS_PATH": str(repo_root / "experiments" / "artifacts" / project),
        "INTERNAL_NETWORK": "true" if isolation == "sovereign" else "false",
    }

    env_content = "\n".join(f"{k}={v}" for k, v in env_vars.items())
    (instance_dir / ".env").write_text(env_content)

    print(f"Launching {project} stack (isolation={isolation})")
    print(f"  MLX server  : http://localhost:{port_base}")
    print(f"  Qdrant      : http://localhost:{port_base + 1}")
    print(f"  MLflow      : http://localhost:{port_base + 2}")
    print(f"  Stack dir   : {instance_dir}")
    if isolation == "sovereign":
        print("  Network     : INTERNAL (sovereign — no external access)")

    result = subprocess.run(
        ["docker", "compose", "up", "-d", "--pull", "missing"],
        cwd=instance_dir,
        check=False,
    )
    if result.returncode != 0:
        print("ERROR: docker compose up failed")
        sys.exit(1)

    # Write port info back to project config for orchestrator routing
    cfg["docker"] = {
        "instance_dir": str(instance_dir),
        "ports": {
            "mlx": port_base,
            "qdrant": port_base + 1,
            "mlflow": port_base + 2,
        },
        "isolation": isolation,
    }
    cfg_path.write_text(yaml.dump(cfg, default_flow_style=False, allow_unicode=True))
    print(f"  Config updated: {cfg_path}")

    return instance_dir


def stop(project: str, remove: bool = False) -> None:
    instance_dir = _INSTANCES_DIR / project
    if not instance_dir.exists():
        print(f"No instance found for project '{project}'")
        return
    cmd = ["docker", "compose", "down"]
    if remove:
        cmd += ["-v", "--remove-orphans"]
    subprocess.run(cmd, cwd=instance_dir, check=True)
    if remove:
        shutil.rmtree(instance_dir)
        print(f"Stack and instance dir removed: {instance_dir}")
    else:
        print(f"Stack stopped (instance dir kept): {instance_dir}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Manage per-project Docker stacks")
    sub = parser.add_subparsers(dest="cmd")

    up = sub.add_parser("up", help="Launch a project stack")
    up.add_argument("--project", required=True)
    up.add_argument("--force", action="store_true", help="Redeploy if already running")

    down = sub.add_parser("down", help="Stop a project stack")
    down.add_argument("--project", required=True)
    down.add_argument("--remove", action="store_true", help="Also remove volumes + instance dir")

    args = parser.parse_args()

    if args.cmd == "up":
        launch(args.project, force=args.force)
    elif args.cmd == "down":
        stop(args.project, remove=args.remove)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
