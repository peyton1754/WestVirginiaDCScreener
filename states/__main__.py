"""
Readiness report for the state registry.

    python3 -m states

Prints what each declared state can do today and, for the ones that cannot run
yet, exactly which field is unfilled. The point is that the remaining work to
add a state is visible and finite rather than discovered when a run fails at
step 11 of 11.
"""
import sys

from . import schema

LEVEL_ORDER = {schema.READY_FULL: 0, schema.READY_PARTIAL: 1, schema.READY_DECLARED: 2}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        configs = schema.load_all()
    except schema.StateConfigError as e:
        print(f"registry is malformed:\n{e}", file=sys.stderr)
        return 2

    if not configs:
        print("No state configs found in states/registry/.", file=sys.stderr)
        return 2

    rows = []
    for code, cfg in configs.items():
        level, reasons = schema.readiness(cfg)
        rows.append((LEVEL_ORDER[level], code, cfg, level, reasons))
    rows.sort()

    counts = {schema.READY_FULL: 0, schema.READY_PARTIAL: 0, schema.READY_DECLARED: 0}
    print(f"{'STATE':<6} {'READY':<9} {'FIPS':<5} {'SCORE':<6} NAME")
    print("-" * 62)
    for _, code, cfg, level, _reasons in rows:
        counts[level] += 1
        print(f"{code:<6} {level:<9} {cfg['fips'] or '-':<5} "
              f"{cfg['state_score'] if cfg['state_score'] is not None else '-':<6} {cfg['name']}")

    print()
    print(f"{counts[schema.READY_FULL]} full, {counts[schema.READY_PARTIAL]} partial, "
          f"{counts[schema.READY_DECLARED]} declared, {len(configs)} total")

    gaps = [(code, reasons) for _, code, _c, level, reasons in rows
            if level != schema.READY_FULL]
    if gaps:
        print("\nWhat each non-full state still needs:")
        for code, reasons in gaps:
            for r in reasons:
                print(f"  {code}: {r}")

    if '--strict' in argv and counts[schema.READY_DECLARED]:
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
