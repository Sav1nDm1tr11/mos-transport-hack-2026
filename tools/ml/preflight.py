"""Check an ML service without inference unless --predict is explicitly selected."""

import argparse
import asyncio
import json
from pathlib import Path

from processing_worker.ml_client import MLClient
from transport_contracts import PredictionBatch


async def inspect(url, batch=None, history_seconds=1800, predict=False, transport=None):
    client = MLClient(url, history_seconds=history_seconds, transport=transport)
    report = dict(ready=False, compatible=False, prediction_attempted=False)
    try:
        info = await client.check()
        report.update(ready=True, model_info=info.model_dump(), compatible=True)
        if batch is not None:
            missing = client.inspect_batch(batch, info)
            report.update(insufficient_targets=missing, compatible=not missing)
            if predict and not missing:
                report["prediction_attempted"] = True
                results = await client.predict(batch)
                report["results"] = [r.model_dump() for r in results]
                report["compatible"] = all(r.status == "ok" for r in results)
    except Exception as exc:
        # Do not leak credentials, URLs or arbitrary remote response bodies.
        report.update(compatible=False, error=type(exc).__name__)
    finally:
        await client.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--history-seconds", type=int, default=1800)
    parser.add_argument("--predict", action="store_true")
    args = parser.parse_args()
    batch = (
        PredictionBatch.model_validate_json(args.batch.read_bytes())
        if args.batch
        else None
    )
    if args.predict and batch is None:
        parser.error("--predict requires --batch")
    report = asyncio.run(inspect(args.url, batch, args.history_seconds, args.predict))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["ready"] and report["compatible"] else 1)


if __name__ == "__main__":
    main()
