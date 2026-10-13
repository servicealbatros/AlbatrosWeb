"""Read-only catalogue evidence and a supervised live probe of an existing request.

The probe starts the existing supplier workers, not a second browser session.
It never calls an approval, WhatsApp, booking or ordering endpoint.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import time
from urllib.request import Request, urlopen

RUNNING = {"running", "queued", "not_started", "identifying"}


def source_report(request_id, vin, paths, catalog_vin=None):
    sources = {}
    accepted_vins = {str(vin).upper()}
    if catalog_vin:
        accepted_vins.add(str(catalog_vin).upper())
    for name, path in paths.items():
        path = Path(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("Rezultat invalid")
            observed_vin = str(data.get("source_vin") or data.get("vin") or "").upper()
            if data.get("request_id") != request_id or not vin or observed_vin not in accepted_vins:
                raise ValueError("Rezultatul nu aparține cererii și VIN-ului curent")
            sources[name] = {
                "status": data.get("status") or "unknown",
                "source": data.get("source") or name,
                "updated_at_ns": path.stat().st_mtime_ns,
                "vehicle": data.get("vehicle") or {},
                "job_id": data.get("job_id"),
                "products": data.get("products") or [],
                "resolved_operations": data.get("resolved_operations") or [],
                "unresolved_operations": data.get("unresolved_operations") or [],
                "detected_operations": data.get("detected_operations") or [],
                "technical_hours": data.get("technical_hours"),
                "missing_parts": data.get("missing_requested_parts") or data.get("missing_product_kinds") or [],
                "message": data.get("message") or "",
                "historical_only": bool(data.get("skipped_due_to_dsm_exact_match") or "DSM" in str(data.get("source") or "")),
            }
        except FileNotFoundError:
            sources[name] = {"status": "not_started", "message": "Sursa nu a publicat încă un rezultat."}
        except (OSError, ValueError, TypeError) as exc:
            sources[name] = {"status": "error", "message": str(exc),
                             "updated_at_ns": path.stat().st_mtime_ns if path.exists() else 0}
    return {"request_id": request_id, "vin": vin, "sources": sources,
            "accuracy_percent": None,
            "review_required": True,
            "message": "Rezultatele sunt dovezi de catalog; acuratețea necesită verificarea pieselor corecte."}


def current_report(report, started_ns):
    """Do not let an earlier successful run pass as this probe's result."""
    report = json.loads(json.dumps(report))
    for name, source in report["sources"].items():
        if source.get("updated_at_ns", 0) < started_ns:
            report["sources"][name] = {"status": "not_started", "message": "Așteptăm rezultatul rulării curente."}
    report["finished"] = all(s.get("status") not in RUNNING for s in report["sources"].values())
    report["all_sources_ready"] = all(s.get("status") == "ready" and not s.get("historical_only")
                                      for s in report["sources"].values())
    return report


def main():
    parser = argparse.ArgumentParser(description="Test live AutoTotal, Inter Cars, TecRMI și Haynes")
    parser.add_argument("request_id", type=int)
    parser.add_argument("--start", action="store_true", help="Repornește căutarea pe cererea de test; resetează selecțiile ofertei")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.request_id <= 0 or args.timeout <= 0:
        parser.error("Cererea și timeout-ul trebuie să fie pozitive")
    base = "http://127.0.0.1:8010/api/requests/" + str(args.request_id)
    def call(url, post=False):
        req = Request(url, data=b"{}" if post else None,
                      headers={"Content-Type": "application/json"}, method="POST" if post else "GET")
        with urlopen(req, timeout=45) as response:
            return json.load(response)
    # Default is inspection only, with no state mutation.
    if not args.start:
        report = call(base + "/catalogue-sources")
    else:
        started_ns = time.time_ns()
        started = call(base + "/offer-start?live_catalogues=true", post=True)
        if not started.get("ok"):
            raise SystemExit(started.get("message") or "Căutarea nu a pornit")
        deadline = time.monotonic() + args.timeout
        while True:
            report = current_report(call(base + "/catalogue-sources"), started_ns)
            if report["finished"] or time.monotonic() >= deadline:
                break
            time.sleep(3)
        report["timed_out"] = not report["finished"]
        report["job_id"] = started.get("job_id")
        observed_job = report["sources"].get("intercars", {}).get("job_id")
        if observed_job and observed_job != started.get("job_id"):
            report["all_sources_ready"] = False
            report["superseded"] = True
        report["started_at_ns"] = started_ns
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
