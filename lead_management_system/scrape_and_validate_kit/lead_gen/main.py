import os
import sys

# Make lead_gen importable no matter where Python was launched from
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utils.envtools import load_env, output_dir

load_env()

from pipeline.runner import run_domain


def main():
    domains_str = os.getenv("TARGET_DOMAINS", "")
    domains = [d.strip() for d in domains_str.split(",") if d.strip()]

    if not domains:
        print("No TARGET_DOMAINS found in .env (see .env.example)")
        return

    print(f"Loaded {len(domains)} target domains from .env")

    results = {}
    for domain in domains:
        results[domain] = run_domain(domain)

    # Print summary
    print("\n\n" + "=" * 50)
    print("PIPELINE SUMMARY")
    print("=" * 50)
    print(f"{'DOMAIN':<30} | {'LEADS WRITTEN':<15}")
    print("-" * 50)
    total = 0
    for domain, count in results.items():
        print(f"{domain:<30} | {count:<15}")
        total += count
    print("-" * 50)
    print(f"{'TOTAL':<30} | {total:<15}")
    print("=" * 50)
    print(f"CSV output folder: {output_dir()}")


if __name__ == "__main__":
    main()
