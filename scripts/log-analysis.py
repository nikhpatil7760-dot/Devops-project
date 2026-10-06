import argparse
import subprocess
import sys


def kubectl(*arguments):
    result = subprocess.run(
        ["kubectl", *arguments], capture_output=True, text=True, check=False
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "kubectl command failed")
    return result.stdout


def main():
    parser = argparse.ArgumentParser(description="Summarize Flask workload log errors.")
    parser.add_argument("--namespace", default="flask-app")
    parser.add_argument("--since", default="1h")
    args = parser.parse_args()

    try:
        pods = kubectl(
            "get",
            "pods",
            "-n",
            args.namespace,
            "-l",
            "app.kubernetes.io/name=flask-app",
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ).split()
        if not pods:
            print(f"No Flask pods found in namespace {args.namespace}.")
            return 1

        terms = ("error", "exception", "failed", "fatal", "traceback")
        for pod in pods:
            logs = kubectl(
                "logs",
                pod,
                "-n",
                args.namespace,
                "--all-containers=true",
                f"--since={args.since}",
            )
            matches = [line for line in logs.splitlines() if any(t in line.lower() for t in terms)]
            print(f"\n=== {pod}: {len(matches)} matching log lines ===")
            print("\n".join(matches[-50:]) if matches else "No common error patterns found.")
        return 0
    except (OSError, RuntimeError) as error:
        print(f"Log analysis failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())